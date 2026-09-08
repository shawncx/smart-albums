"""Manual synthetic benchmark, run from the checkout; no models or user albums.

python tests/benchmark_duplicates.py --output artifacts/duplicates-benchmark.json
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "photography" / "scripts"))
from photography_lib import duplicates


def peak_memory_mb():
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                   "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                   "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        query.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return round(counters.PeakWorkingSetSize / 1024 ** 2, 2)
    import resource
    scale = 1024 ** 2 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2)


def case(size, kind):
    rng = random.Random(9102026)
    exact = kind == "exact"
    parameters = {"mode": "exact" if exact else "similar", "max_distance": None if exact else 8,
                  "profile_id": None if exact else "0" * 64, "algorithm": duplicates.ALGORITHM,
                  "grouping": duplicates.GROUPING}
    inputs = []
    for i in range(size):
        value = 0 if kind == "dense" else rng.getrandbits(64)
        inputs.append({"photo_id": f"photo_{i:06}",
                       "content_version": hashlib.sha256(("same" if exact else str(i)).encode()).hexdigest(),
                       "hash": None if exact else {"hash_hex": f"{value:016x}", "profile_id": "0" * 64,
                                                    "result_id": f"fixture-{i}", "input_fingerprint": "0" * 64,
                                                    "payload_hash": "0" * 64},
                       "exact_status": "eligible" if exact else "not_requested",
                       "similar_status": "not_requested" if exact else "eligible"})
    started = time.perf_counter()
    groups, matches = duplicates._build_groups(inputs, parameters)
    elapsed = time.perf_counter() - started
    evidence_bytes = len(json.dumps({"inputs": inputs, "groups": groups, "matches": matches}).encode())
    return {"photos": size, "kind": kind, "matching_seconds": round(elapsed, 4),
            "peak_process_mb": peak_memory_mb(), "serialized_evidence_bytes": evidence_bytes,
            "groups": len(groups), "pairs": sum(g["pair_counts"]["total"] for g in groups)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", choices=("exact", "sparse", "dense"))
    parser.add_argument("--size", type=int)
    args = parser.parse_args()
    if args.case:
        print(json.dumps(case(args.size, args.case)))
        return
    if args.output is None:
        parser.error("Use --output for the benchmark results.")
    results = []
    for size, kind in ((1000, "sparse"), (5000, "sparse"), (10000, "sparse"), (10000, "exact"), (1000, "dense")):
        raw = subprocess.check_output([sys.executable, __file__, "--case", kind, "--size", str(size)], text=True)
        row = json.loads(raw)
        results.append(row)
        print(json.dumps(row), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"source": "Deterministic synthetic SHA/dHash values, seed 9102026",
                                      "scope": "Matching/grouping kernel only; SQLite reads and HTML export excluded",
                                      "python": sys.version, "platform": platform.platform(), "cases": results}, indent=2),
                           encoding="utf-8")


if __name__ == "__main__":
    main()
