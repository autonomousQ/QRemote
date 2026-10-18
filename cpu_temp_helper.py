"""Reads CPU package temperature and power (AMD Ryzen or Intel) through the PawnIO driver and writes them to a file.

PawnIO only accepts admin callers, so setup.ps1 runs this as SYSTEM at startup from
Program Files (admin-only writable). The non-admin bot just reads OUT_PATH.
"""
import ctypes
import json
import os
import sys
import time
import winreg
from ctypes import wintypes

PAWNIO_DLL = r"C:\Program Files\PawnIO\PawnIOLib.dll"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "QRemote", "cpu_temp.json")
INTERVAL_SECS = 10
# AMD (family 17h-1Ah, PawnIO module AMDFamily17.bin)
THM_TCON_CUR_TMP = 0x00059800  # SMN register
MSR_PWR_UNIT = 0xC0010299
MSR_PKG_ENERGY_STAT = 0xC001029B
# Intel (PawnIO module IntelMSR.bin)
MSR_IA32_PACKAGE_THERM_STATUS = 0x1B1
MSR_IA32_TEMPERATURE_TARGET = 0x1A2
MSR_RAPL_POWER_UNIT = 0x606
MSR_PKG_ENERGY_STATUS = 0x611


def decode_tctl(raw: int) -> float:
    # Bits [31:21] = temp in 0.125 °C steps; bit 19 set = range shifted by -49 °C (same as Linux k10temp / LHM)
    temp = (raw >> 21) * 0.125
    return round(temp - 49 if raw & 0x80000 else temp, 1)


def intel_temp(tjmax: int, pkg_therm_status: int) -> float:
    # Bits [22:16] = degrees below TjMax (same as Linux coretemp / LHM)
    return float(tjmax - ((pkg_therm_status >> 16) & 0x7F))


def intel_tjmax(temperature_target: int) -> int:
    return ((temperature_target >> 16) & 0xFF) or 100  # bits [23:16]; 0 on some VMs/old parts, 100 is the usual value


def energy_unit(pwr_unit: int) -> float:
    return 1 / (1 << ((pwr_unit >> 8) & 0x1F))  # bits [12:8] = energy status unit, joules = 1/2^ESU


def watts(prev: int, cur: int, unit: float, secs: float) -> float:
    return round(((cur - prev) & 0xFFFFFFFF) * unit / secs, 1)  # 32-bit counter, mask handles wrap


def cpu_vendor() -> str:
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
    return winreg.QueryValueEx(key, "VendorIdentifier")[0]  # "AuthenticAMD" / "GenuineIntel"


class PawnIO:
    def __init__(self, module_path: str):
        self.lib = ctypes.WinDLL(PAWNIO_DLL)
        self.handle = wintypes.HANDLE()
        self._check(self.lib.pawnio_open(ctypes.byref(self.handle)), "pawnio_open")
        blob = open(module_path, "rb").read()
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

    def _execute(self, fn: bytes, arg: int) -> int:
        inp = (ctypes.c_uint64 * 1)(arg)
        out = (ctypes.c_uint64 * 1)()
        n = ctypes.c_size_t()
        self.k32.WaitForSingleObject(self.mutex, 1000)
        try:
            hr = self.lib.pawnio_execute(self.handle, fn, inp, ctypes.c_size_t(1),
                                         out, ctypes.c_size_t(1), ctypes.byref(n))
        finally:
            self.k32.ReleaseMutex(self.mutex)
        self._check(hr, fn.decode())
        return out[0] & 0xFFFFFFFF  # SMN values and the MSRs used here only matter in the low 32 bits

    def read_smn(self, addr: int) -> int:
        return self._execute(b"ioctl_read_smn", addr)

    def read_msr(self, msr: int) -> int:
        return self._execute(b"ioctl_read_msr", msr)


def write_atomic(data: dict) -> None:
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, OUT_PATH)


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    try:
        vendor = cpu_vendor()
        if vendor == "AuthenticAMD":
            pawn = PawnIO(os.path.join(HERE, "AMDFamily17.bin"))
            read_temp = lambda: decode_tctl(pawn.read_smn(THM_TCON_CUR_TMP))
            unit_msr, energy_msr = MSR_PWR_UNIT, MSR_PKG_ENERGY_STAT
        elif vendor == "GenuineIntel":
            pawn = PawnIO(os.path.join(HERE, "IntelMSR.bin"))
            tjmax = intel_tjmax(pawn.read_msr(MSR_IA32_TEMPERATURE_TARGET))
            read_temp = lambda: intel_temp(tjmax, pawn.read_msr(MSR_IA32_PACKAGE_THERM_STATUS))
            unit_msr, energy_msr = MSR_RAPL_POWER_UNIT, MSR_PKG_ENERGY_STATUS
        else:
            raise OSError(f"unsupported CPU vendor {vendor}")
        unit = energy_unit(pawn.read_msr(unit_msr))
        prev = (pawn.read_msr(energy_msr), time.monotonic())
    except OSError as exc:
        # Runs hidden under pythonw, so leave the reason where `type %ProgramData%\QRemote\cpu_temp.json` shows it
        write_atomic({"t": int(time.time()), "cpu_temp": None, "cpu_power": None, "error": f"startup: {exc}"})
        raise
    while True:
        time.sleep(INTERVAL_SECS)
        try:
            cur = (pawn.read_msr(energy_msr), time.monotonic())
            power = watts(prev[0], cur[0], unit, cur[1] - prev[1])
            prev = cur
            write_atomic({"t": int(time.time()), "cpu_temp": read_temp(), "cpu_power": power})
        except OSError as exc:
            write_atomic({"t": int(time.time()), "cpu_temp": None, "cpu_power": None, "error": str(exc)})


if __name__ == "__main__":
    if "--test" in sys.argv:
        assert decode_tctl(372 << 21) == 46.5  # 372 * 0.125
        assert decode_tctl(372 << 21 | 0x80000) == -2.5
        assert energy_unit(0x000A1003) == 1 / 65536  # ESU = 16, typical Zen value
        assert watts(1000, 1000 + 65536 * 500, 1 / 65536, 10) == 50.0
        assert intel_tjmax(0x00640000) == 100 and intel_tjmax(0) == 100
        assert intel_temp(100, 0x88370000) == 45.0  # readout 0x37 = 55 below TjMax
        assert energy_unit(0x000A0E03) == 1 / 16384  # ESU = 14, typical Intel value
        assert watts(0xFFFFFFFF - 65536 * 100 + 1, 65536 * 400, 1 / 65536, 10) == 50.0  # counter wrapped
        print("ok")
    else:
        main()
