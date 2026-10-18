"""Reads AMD Ryzen CPU temperature (Tctl) through the PawnIO driver and writes it to a file.

PawnIO only accepts admin callers, so setup.ps1 runs this as SYSTEM at startup from
Program Files (admin-only writable). The non-admin bot just reads OUT_PATH.
"""
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

PAWNIO_DLL = r"C:\Program Files\PawnIO\PawnIOLib.dll"
MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AMDFamily17.bin")
OUT_PATH = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "QRemote", "cpu_temp.json")
INTERVAL_SECS = 10
THM_TCON_CUR_TMP = 0x00059800  # SMN register, AMD family 17h-1Ah


def decode_tctl(raw: int) -> float:
    # Bits [31:21] = temp in 0.125 °C steps; bit 19 set = range shifted by -49 °C (same as Linux k10temp / LHM)
    temp = (raw >> 21) * 0.125
    return round(temp - 49 if raw & 0x80000 else temp, 1)


class PawnIO:
    def __init__(self):
        self.lib = ctypes.WinDLL(PAWNIO_DLL)
        self.handle = wintypes.HANDLE()
        self._check(self.lib.pawnio_open(ctypes.byref(self.handle)), "pawnio_open")
        blob = open(MODULE_PATH, "rb").read()
        self._check(self.lib.pawnio_load(self.handle, blob, ctypes.c_size_t(len(blob))), "pawnio_load")
        # Shared with LHM/HWiNFO etc. so two tools never interleave SMN index/data writes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        self.k32 = k32
        self.mutex = k32.CreateMutexW(None, False, "Global\\Access_PCI")

    @staticmethod
    def _check(hr: int, what: str) -> None:
        if hr & 0x80000000:
            raise OSError(f"{what} failed: HRESULT 0x{hr & 0xFFFFFFFF:08x}")

    def read_smn(self, addr: int) -> int:
        inp = (ctypes.c_uint64 * 1)(addr)
        out = (ctypes.c_uint64 * 1)()
        n = ctypes.c_size_t()
        self.k32.WaitForSingleObject(self.mutex, 1000)
        try:
            hr = self.lib.pawnio_execute(self.handle, b"ioctl_read_smn", inp, ctypes.c_size_t(1),
                                         out, ctypes.c_size_t(1), ctypes.byref(n))
        finally:
            self.k32.ReleaseMutex(self.mutex)
        self._check(hr, "ioctl_read_smn")
        return out[0] & 0xFFFFFFFF


def write_atomic(data: dict) -> None:
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    pawn = PawnIO()
    while True:
        try:
            write_atomic({"t": int(time.time()), "cpu_temp": decode_tctl(pawn.read_smn(THM_TCON_CUR_TMP))})
        except OSError as exc:
            write_atomic({"t": int(time.time()), "cpu_temp": None, "error": str(exc)})
        time.sleep(INTERVAL_SECS)


if __name__ == "__main__":
    if "--test" in sys.argv:
        assert decode_tctl(372 << 21) == 46.5  # 372 * 0.125
        assert decode_tctl(372 << 21 | 0x80000) == -2.5
        print("ok")
    else:
        main()
