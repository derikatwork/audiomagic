import asyncio
import time

import aiohttp
import numpy as np
import pytest

from audiomagic.config import Settings
from audiomagic.engine import Engine
from audiomagic.project import ProjectStore
from audiomagic.server import Server


@pytest.fixture
def server(tmp_path):
    e = Engine(store=ProjectStore(str(tmp_path / "p")), settings=Settings(str(tmp_path / "s.json")))
    e.start()
    s = Server(e)
    s.start_in_thread()
    yield s
    e.shutdown()
    s.stop()


def run(coro):
    return asyncio.run(coro)


async def api(session, s, method, path, body=None, token=True, headers=None):
    h = {"X-AudioMagic-Token": s.token} if token else {}
    h.update(headers or {})
    async with session.request(method, f"{s.url.rstrip('/')}{path}", json=body, headers=h) as r:
        ctype = r.headers.get("Content-Type", "")
        data = await (r.json() if "json" in ctype else r.read())
        return r.status, data


def test_security_checks(server):
    async def go():
        async with aiohttp.ClientSession() as ses:
            assert (await api(ses, server, "GET", "/api/state", token=False))[0] == 401
            st, _ = await api(ses, server, "GET", "/api/state", headers={"X-AudioMagic-Token": "nope"}, token=False)
            assert st == 401
            st, _ = await api(ses, server, "GET", "/api/state", headers={"Host": "evil.example:80"})
            assert st == 403
            st, _ = await api(ses, server, "POST", "/api/record/start", headers={"Origin": "http://evil.example"})
            assert st == 403
            st, body = await api(ses, server, "GET", "/")
            assert st == 200 and b"AudioMagic" in body
            st, data = await api(ses, server, "GET", "/api/state")
            assert st == 200 and data["project"]["name"] == "My First Project"
    run(go())


def test_record_edit_export_over_api(server, tmp_path):
    async def go():
        async with aiohttp.ClientSession() as ses:
            st, r = await api(ses, server, "POST", "/api/tracks",
                              {"items": [{"source": {"kind": "tone", "freq": 700}, "name": "Beep"}]})
            assert st == 200
            tid = r["ids"][0]
            st, r = await api(ses, server, "PATCH", f"/api/tracks/{tid}",
                              {"gain_db": -6, "fx": {"eq": {"on": True, "mid": 3}}})
            assert st == 200
            st, r = await api(ses, server, "PATCH", f"/api/tracks/{tid}", {"gain_db": "loud"})
            assert st == 400

            ws = await ses.ws_connect(f"{server.url}ws?token={server.token}")
            first = await ws.receive_json(timeout=5)
            assert first["type"] == "state"
            await asyncio.sleep(0.8)  # tone input starting
            st, r = await api(ses, server, "POST", "/api/record/start")
            assert st == 200
            take = r["take"]
            seen = set()
            t_end = time.time() + 1.5
            while time.time() < t_end:
                msg = await ws.receive_json(timeout=2)
                seen.add(msg["type"])
                if msg["type"] == "meters" and msg["rec"] is not None:
                    seen.add("recording-meters")
            assert {"recording-meters", "recpeaks"} <= seen
            st, r = await api(ses, server, "POST", "/api/record/stop")
            assert st == 200 and r["take"] == take

            st, state = await api(ses, server, "GET", "/api/state")
            tk = next(t for t in state["takes"] if t["id"] == take)
            assert tk["duration"] > 48000

            st, pk = await api(ses, server, "GET", f"/api/takes/{take}/peaks/{tid}")
            assert st == 200 and len(pk) > 300
            assert np.frombuffer(pk, np.int8).max() > 50

            st, r = await api(ses, server, "POST", f"/api/takes/{take}/edit", {"op": "cut", "start": 0.1, "end": 0.1})
            assert st == 400 and "Select" in r["error"]
            st, r = await api(ses, server, "POST", f"/api/takes/{take}/edit", {"op": "cut", "start": 0.1, "end": 0.3})
            assert st == 200
            st, r = await api(ses, server, "PATCH", f"/api/takes/{take}", {"name": "Intro"})
            assert st == 200

            st, opts = await api(ses, server, "GET", "/api/export/options")
            assert {f["id"] for f in opts["formats"]} >= {"flac", "mp3", "opus", "vorbis"}
            st, r = await api(ses, server, "POST", "/api/export",
                              {"take": take, "format": "opus", "what": "mix", "folder": str(tmp_path / "ex")})
            assert st == 200
            job = r["job"]
            done = None
            t_end = time.time() + 30
            while time.time() < t_end and done is None:
                msg = await ws.receive_json(timeout=30)
                if msg["type"] == "export" and msg["job"]["id"] == job and msg["job"]["state"] != "running":
                    done = msg["job"]
            assert done and done["state"] == "done", done
            assert done["files"][0].endswith("Intro.opus")

            st, r = await api(ses, server, "DELETE", f"/api/tracks/{tid}")
            assert st == 409 and "trash" in r["confirm"]
            st, r = await api(ses, server, "DELETE", f"/api/tracks/{tid}?confirm=1")
            assert st == 200
            await ws.close()
    run(go())
