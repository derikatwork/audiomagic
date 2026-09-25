"""WAV reading and writing.

Recordings are written as 24-bit PCM. The header is rewritten about once a
second so that a crash or power cut leaves a playable file behind, and
``repair()`` fixes the size fields of any file that was cut off mid-write.
"""

import os
import struct
import threading

import numpy as np

_FMT_PCM = 1
_FMT_FLOAT = 3
_FMT_EXTENSIBLE = 0xFFFE


def _header(channels, rate, bits, fmt_tag, data_bytes):
    block_align = channels * bits // 8
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", min(0xFFFFFFFF, 36 + data_bytes)),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, fmt_tag, channels, rate, rate * block_align, block_align, bits),
            b"data",
            struct.pack("<I", min(0xFFFFFFFF, data_bytes)),
        ]
    )


def float_to_pcm24(x):
    """float32 array (any shape) -> little-endian 24-bit bytes."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    ints = np.rint(np.clip(x, -1.0, 1.0) * 8388607.0).astype("<i4")
    return ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


def pcm24_to_float(raw):
    raw = np.frombuffer(raw, dtype=np.uint8)
    n = raw.size // 3
    padded = np.empty(raw.size + 1, np.uint8)
    padded[0] = 0
    padded[1:] = raw
    # read each sample as an int32 whose top 3 bytes are the sample (the low
    # byte is the previous sample's last byte), then shift it back down,
    # which also sign-extends
    ints = np.ndarray((n,), dtype="<i4", buffer=padded, strides=(3,)) >> 8
    out = ints.astype(np.float32)
    out *= 1.0 / 8388608.0
    return out


class WavWriter:
    """Streams float audio into a 24-bit PCM WAV file."""

    HEADER_BYTES = 44

    def __init__(self, path, channels, rate=48000, bits=24, sync_interval=1.0):
        if bits not in (16, 24):
            raise ValueError("bits must be 16 or 24")
        self.path = path
        self.channels = channels
        self.rate = rate
        self.bits = bits
        self.frames = 0
        self._lock = threading.Lock()
        self._bytes_per_frame = channels * bits // 8
        self._since_sync = 0
        self._sync_frames = int(rate * sync_interval)
        self._since_fsync = 0
        self._f = open(path, "wb")
        self._f.write(_header(channels, rate, bits, _FMT_PCM, 0))
        self._f.flush()

    def write(self, block):
        block = np.asarray(block, dtype=np.float32)
        if block.ndim == 1:
            block = block.reshape(-1, 1)
        if block.shape[1] != self.channels:
            raise ValueError(f"expected {self.channels} channels, got {block.shape[1]}")
        n = block.shape[0]
        if n == 0:
            return
        if self.bits == 24:
            data = float_to_pcm24(block)
        else:
            data = np.rint(np.clip(block, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        with self._lock:
            if self._f is None:
                return
            self._f.write(data)
            self.frames += n
            self._since_sync += n
            if self._since_sync >= self._sync_frames:
                self._since_sync = 0
                self._update_header()
                self._since_fsync += 1
                if self._since_fsync >= 10:
                    self._since_fsync = 0
                    os.fsync(self._f.fileno())

    def write_silence(self, frames):
        while frames > 0:
            n = min(frames, 48000)
            self.write(np.zeros((n, self.channels), dtype=np.float32))
            frames -= n

    def truncate(self, frames):
        """Drop everything after ``frames`` (used to make all tracks of a take equally long)."""
        with self._lock:
            if self._f is None or frames >= self.frames:
                return
            self.frames = max(0, frames)
            self._f.seek(self.HEADER_BYTES + self.frames * self._bytes_per_frame)
            self._f.truncate()
            self._update_header()

    def _update_header(self):
        data_bytes = self.frames * self._bytes_per_frame
        pos = self._f.tell()
        self._f.seek(4)
        self._f.write(struct.pack("<I", min(0xFFFFFFFF, 36 + data_bytes)))
        self._f.seek(40)
        self._f.write(struct.pack("<I", min(0xFFFFFFFF, data_bytes)))
        self._f.seek(pos)
        self._f.flush()

    def close(self):
        with self._lock:
            if self._f is None:
                return
            self._update_header()
            os.fsync(self._f.fileno())
            self._f.close()
            self._f = None


class WavInfo:
    def __init__(self, fmt_tag, channels, rate, bits, data_offset, data_bytes):
        self.fmt_tag = fmt_tag
        self.channels = channels
        self.rate = rate
        self.bits = bits
        self.data_offset = data_offset
        self.data_bytes = data_bytes

    @property
    def frame_bytes(self):
        return self.channels * self.bits // 8

    @property
    def frames(self):
        return self.data_bytes // self.frame_bytes


def read_info(path):
    """Parse a WAV header. Size fields that disagree with the file size
    (a recording that was cut off) are corrected from the real file size."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] not in (b"RIFF", b"RF64") or riff[8:12] != b"WAVE":
            raise ValueError(f"{path} is not a WAV file")
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, clen = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                body = f.read(clen)
                tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == _FMT_EXTENSIBLE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]
                fmt = (tag, ch, rate, bits)
                if clen % 2:
                    f.read(1)
            elif cid == b"data":
                if fmt is None:
                    raise ValueError(f"{path}: data before fmt")
                off = f.tell()
                avail = size - off
                if clen == 0 or clen == 0xFFFFFFFF or clen > avail:
                    clen = avail
                info = WavInfo(fmt[0], fmt[1], fmt[2], fmt[3], off, clen)
                info.data_bytes -= info.data_bytes % info.frame_bytes
                return info
            else:
                f.seek(clen + (clen % 2), 1)
    raise ValueError(f"{path}: no audio data found")


def repair(path):
    """Rewrite the size fields of a 24/16-bit PCM WAV written by WavWriter.
    Returns the number of frames in the file."""
    info = read_info(path)
    if info.data_offset == WavWriter.HEADER_BYTES:
        with open(path, "r+b") as f:
            f.seek(4)
            f.write(struct.pack("<I", min(0xFFFFFFFF, 36 + info.data_bytes)))
            f.seek(40)
            f.write(struct.pack("<I", min(0xFFFFFFFF, info.data_bytes)))
    return info.frames


class WavReader:
    """Random-access reader returning float32 frames of shape (n, channels)."""

    def __init__(self, path):
        self.path = path
        self.info = read_info(path)
        if self.info.fmt_tag == _FMT_PCM and self.info.bits in (16, 24, 32):
            pass
        elif self.info.fmt_tag == _FMT_FLOAT and self.info.bits == 32:
            pass
        else:
            raise ValueError(f"unsupported WAV format in {path}")
        self.channels = self.info.channels
        self.rate = self.info.rate
        self.frames = self.info.frames
        if self.info.data_bytes:
            self._mm = np.memmap(path, dtype=np.uint8, mode="r", offset=self.info.data_offset, shape=(self.info.data_bytes,))
        else:
            self._mm = np.zeros(0, dtype=np.uint8)

    def read(self, start, n):
        """Frames [start, start+n); anything outside the file is silence."""
        out = np.zeros((n, self.channels), dtype=np.float32)
        a = max(0, start)
        b = min(self.frames, start + n)
        if b <= a:
            return out
        fb = self.info.frame_bytes
        raw = self._mm[a * fb:b * fb]
        bits, tag = self.info.bits, self.info.fmt_tag
        if tag == _FMT_FLOAT:
            vals = np.frombuffer(raw.tobytes(), dtype="<f4")
        elif bits == 24:
            vals = pcm24_to_float(raw)
        elif bits == 16:
            vals = np.frombuffer(raw.tobytes(), dtype="<i2").astype(np.float32) / 32768.0
        else:
            vals = np.frombuffer(raw.tobytes(), dtype="<i4").astype(np.float32) / 2147483648.0
        out[a - start:b - start] = vals.reshape(-1, self.channels)
        return out

    def close(self):
        self._mm = None
