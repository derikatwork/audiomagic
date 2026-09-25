"""The local HTTP/WebSocket server behind the app window.

It listens on 127.0.0.1 only. Every API call and the WebSocket need a random
token that is handed to the window at start-up, and requests whose Host or
Origin isn't our own address are refused, so web pages open in a browser
can't reach the app.
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from aiohttp import WSMsgType, web

from . import export as export_mod
from .engine import NeedsConfirm, UserError
from .util import log

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class Server:
    def __init__(self, engine, host="127.0.0.1", port=0):
        self.engine = engine
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine")
        # waveform data is read-only and can be slow for long takes: keep it off the engine thread
        self.peaks_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="peaks")
        self.clients = set()
        self.loop = None
        self.runner = None
        self._thread = None
        self._ready = threading.Event()
        self._stopping = None
        self.app = self._make_app()

    # ------------------------------------------------------------- plumbing
    @property
    def url(self):
        return f"http://{self.host}:{self.port}/"

    def _allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @web.middleware
    async def guard(self, request, handler):
        if request.host not in self._allowed_hosts():
            return web.Response(status=403, text="forbidden host")
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") not in {f"http://{h}" for h in self._allowed_hosts()}:
            return web.Response(status=403, text="forbidden origin")
        path = request.path
        if path.startswith("/api/") or path == "/ws":
            token = request.headers.get("X-AudioMagic-Token") or request.query.get("token")
            if not token or not secrets.compare_digest(token, self.token):
                return web.json_response({"error": "not authorised"}, status=401)
        try:
            return await handler(request)
        except UserError as e:
            return web.json_response({"error": str(e)}, status=400)
        except NeedsConfirm as e:
            return web.json_response({"confirm": str(e)}, status=409)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            log.info("bad request %s: %s", path, e)
            return web.json_response({"error": "Invalid request"}, status=400)
        except web.HTTPException:
            raise
        except Exception as e:
            log.exception("request %s failed", path)
            return web.json_response({"error": f"Something went wrong: {e}"}, status=500)

    async def call(self, fn, *args, **kw):
        return await asyncio.get_running_loop().run_in_executor(self.executor, lambda: fn(*args, **kw))

    @staticmethod
    async def body(request):
        if not request.can_read_body:
            return {}
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        return data

    def _make_app(self):
        app = web.Application(middlewares=[self.guard], client_max_size=1024 * 1024)
        r = app.router
        r.add_get("/", self.index)
        r.add_static("/static/", WEB_DIR, show_index=False)
        r.add_get("/ws", self.ws)
        r.add_get("/api/state", self.h_state)
        r.add_get("/api/sources", self.h_sources)
        r.add_get("/api/projects", self.h_projects)
        r.add_post("/api/projects", self.h_project_create)
        r.add_post("/api/projects/open", self.h_project_open)
        r.add_post("/api/project/rename", self.h_project_rename)
        r.add_post("/api/tracks", self.h_tracks_add)
        r.add_patch("/api/tracks/{id}", self.h_track_update)
        r.add_delete("/api/tracks/{id}", self.h_track_remove)
        r.add_post("/api/tracks/{id}/move", self.h_track_move)
        r.add_post("/api/master", self.h_master)
        r.add_post("/api/output", self.h_output)
        r.add_post("/api/record/start", self.h_rec_start)
        r.add_post("/api/record/stop", self.h_rec_stop)
        r.add_post("/api/play", self.h_play)
        r.add_post("/api/pause", self.h_pause)
        r.add_post("/api/resume", self.h_resume)
        r.add_post("/api/stop", self.h_stop)
        r.add_patch("/api/takes/{id}", self.h_take_rename)
        r.add_delete("/api/takes/{id}", self.h_take_delete)
        r.add_post("/api/takes/{id}/edit", self.h_take_edit)
        r.add_get("/api/takes/{id}/peaks/{track}", self.h_peaks)
        r.add_get("/api/export/options", self.h_export_options)
        r.add_post("/api/export", self.h_export)
        r.add_get("/api/export/{job}", self.h_export_status)
        r.add_post("/api/export/{job}/cancel", self.h_export_cancel)
        r.add_post("/api/open-folder", self.h_open_folder)
        return app

    # ------------------------------------------------------------- handlers
    async def index(self, request):
        resp = web.FileResponse(os.path.join(WEB_DIR, "index.html"))
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'")
        return resp

    async def h_state(self, request):
        return web.json_response(await self.call(self.engine.state))

    async def h_sources(self, request):
        return web.json_response(await self.call(self.engine.list_sources))

    async def h_projects(self, request):
        return web.json_response(await self.call(self.engine.list_projects))

    async def h_project_create(self, request):
        b = await self.body(request)
        path = await self.call(self.engine.create_project, b.get("name"), bool(b.get("copy_inputs")))
        return web.json_response({"path": path})

    async def h_project_open(self, request):
        b = await self.body(request)
        await self.call(self.engine.open_project, str(b["path"]))
        return web.json_response({"ok": True})

    async def h_project_rename(self, request):
        b = await self.body(request)
        await self.call(self.engine.rename_project, b.get("name"))
        return web.json_response({"ok": True})

    async def h_tracks_add(self, request):
        b = await self.body(request)
        items = b.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("items")
        return web.json_response({"ids": await self.call(self.engine.add_tracks, items)})

    async def h_track_update(self, request):
        b = await self.body(request)
        await self.call(self.engine.update_track, request.match_info["id"], b)
        return web.json_response({"ok": True})

    async def h_track_remove(self, request):
        confirm = request.query.get("confirm") in ("1", "true")
        await self.call(self.engine.remove_track, request.match_info["id"], confirm)
        return web.json_response({"ok": True})

    async def h_track_move(self, request):
        b = await self.body(request)
        await self.call(self.engine.move_track, request.match_info["id"], int(b["index"]))
        return web.json_response({"ok": True})

    async def h_master(self, request):
        b = await self.body(request)
        await self.call(self.engine.set_master, float(b["gain_db"]))
        return web.json_response({"ok": True})

    async def h_output(self, request):
        b = await self.body(request)
        await self.call(self.engine.set_output, b.get("node"))
        return web.json_response({"ok": True})

    async def h_rec_start(self, request):
        return web.json_response({"take": await self.call(self.engine.start_recording)})

    async def h_rec_stop(self, request):
        return web.json_response({"take": await self.call(self.engine.stop_recording)})

    async def h_play(self, request):
        b = await self.body(request)
        ok = await self.call(self.engine.play, str(b["take"]), float(b.get("pos", 0)))
        return web.json_response({"ok": ok})

    async def h_pause(self, request):
        await self.call(self.engine.pause)
        return web.json_response({"ok": True})

    async def h_resume(self, request):
        await self.call(self.engine.resume)
        return web.json_response({"ok": True})

    async def h_stop(self, request):
        await self.call(self.engine.stop_playback)
        return web.json_response({"ok": True})

    async def h_take_rename(self, request):
        b = await self.body(request)
        await self.call(self.engine.rename_take, request.match_info["id"], b.get("name", ""))
        return web.json_response({"ok": True})

    async def h_take_delete(self, request):
        await self.call(self.engine.delete_take, request.match_info["id"])
        return web.json_response({"ok": True})

    async def h_take_edit(self, request):
        b = await self.body(request)
        op = str(b.pop("op"))
        await self.call(self.engine.edit_take, request.match_info["id"], op, b)
        return web.json_response({"ok": True})

    async def h_peaks(self, request):
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(self.peaks_executor, self.engine.peaks,
                                          request.match_info["id"], request.match_info["track"])
        return web.Response(body=data, content_type="application/octet-stream",
                            headers={"Cache-Control": "no-store"})

    async def h_export_options(self, request):
        return web.json_response(export_mod.describe())

    async def h_export(self, request):
        b = await self.body(request)
        take = str(b.pop("take"))
        opts = {k: b[k] for k in ("format", "quality", "what", "channels", "normalize", "tags", "folder", "track_ids")
                if k in b}
        return web.json_response({"job": await self.call(self.engine.start_export, take, opts)})

    async def h_export_status(self, request):
        info = self.engine.jobs.get(request.match_info["job"])
        if info is None:
            raise UserError("No such export")
        return web.json_response({k: v for k, v in info.items() if k != "job"})

    async def h_export_cancel(self, request):
        await self.call(self.engine.cancel_export, request.match_info["job"])
        return web.json_response({"ok": True})

    async def h_open_folder(self, request):
        b = await self.body(request)
        path = os.path.realpath(os.path.expanduser(str(b.get("path") or self.engine.project.path)))
        if not os.path.isdir(path):
            raise UserError("That folder doesn't exist any more")
        opener = shutil.which("xdg-open")
        if not opener:
            raise UserError(f"Can't open folders here. The files are in {path}")
        subprocess.Popen([opener, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return web.json_response({"ok": True})

    # ------------------------------------------------------------ websocket
    async def ws(self, request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_json({"type": "state", "state": await self.call(self.engine.state)})
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.clients.discard(ws)
        return ws

    async def broadcast(self, payload):
        if not self.clients:
            return
        text = json.dumps(payload)
        for ws in list(self.clients):
            try:
                # a stuck client must not hold up everyone else
                await asyncio.wait_for(ws.send_str(text), timeout=2)
            except (ConnectionError, RuntimeError, asyncio.TimeoutError):
                self.clients.discard(ws)
                asyncio.ensure_future(ws.close())

    async def pump(self):
        """Meters at ~25 fps, plus events. Never waits for the engine thread,
        so meters keep moving while a slow operation runs."""
        tick = 0
        while True:
            await asyncio.sleep(0.04)
            tick += 1
            try:
                await self.broadcast(self.engine.meters())
                if tick % 3 == 0:
                    peaks = self.engine.rec_peaks()
                    if peaks:
                        await self.broadcast({"type": "recpeaks", "tracks": peaks})
                for ev in self.engine.pop_events():
                    await self.broadcast(ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("update loop failed")

    async def housekeeping(self):
        """Engine upkeep and state updates, on the engine thread."""
        while True:
            await asyncio.sleep(0.1)
            try:
                await self.call(self.engine.poll)
                if self.engine.take_dirty():
                    await self.broadcast({"type": "state", "state": await self.call(self.engine.state)})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")

    # ------------------------------------------------------------ lifecycle
    def start_in_thread(self):
        self._thread = threading.Thread(target=self._run, name="server", daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("the local server did not start")

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self._stopping = asyncio.Event()
        tasks = [asyncio.ensure_future(self.pump()), asyncio.ensure_future(self.housekeeping())]
        self._ready.set()
        await self._stopping.wait()
        for t in tasks:
            t.cancel()
        for ws in list(self.clients):
            await ws.close()
        await self.runner.cleanup()

    def stop(self):
        if self.loop is not None and self._stopping is not None:
            self.loop.call_soon_threadsafe(self._stopping.set)
        if self._thread is not None:
            self._thread.join(timeout=5)
        # let an engine call that is already running (e.g. stopping a recording) finish
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.peaks_executor.shutdown(wait=False, cancel_futures=True)
