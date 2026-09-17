#!/usr/bin/env python3
"""
Hardware2Code Unit Test Runner (with optional coverage support)
Compiles and runs all test executables using native gcc.

Runs every test before reporting: a single compile/run failure no longer
hides the rest (see the comment in main()).
Usage: python run_tests.py [--coverage] [--gcc-path PATH]
GCC path can be set via:
  1. --gcc-path command line argument
  2. H2C_GCC_PATH environment variable
  3. Automatic search in PATH
  4. Default: 'gcc' (Linux/macOS) or 'C:/mingw64/bin/gcc' (Windows)
"""
import subprocess
import sys
import os
import glob
import argparse
import shutil
from pathlib import Path


def find_gcc():
    env_gcc = os.environ.get("H2C_GCC_PATH")
    if env_gcc and shutil.which(env_gcc):
        return env_gcc

    gcc_in_path = shutil.which("gcc")
    if gcc_in_path:
        return gcc_in_path

    if sys.platform.startswith("win"):
        default_paths = [
            "C:/mingw64/bin/gcc",
            "C:/mingw32/bin/gcc",
            "C:/Program Files/mingw-w64/bin/gcc",
        ]
        for path in default_paths:
            if os.path.exists(path):
                return path
        return "gcc"
    else:
        return "gcc"


def find_gcov():
    env_gcov = os.environ.get("H2C_GCOV_PATH")
    if env_gcov and shutil.which(env_gcov):
        return env_gcov

    gcov_in_path = shutil.which("gcov")
    if gcov_in_path:
        return gcov_in_path

    if sys.platform.startswith("win"):
        default_paths = [
            "C:/mingw64/bin/gcov",
            "C:/mingw32/bin/gcov",
            "C:/Program Files/mingw-w64/bin/gcov",
        ]
        for path in default_paths:
            if os.path.exists(path):
                return path
        return "gcov"
    else:
        return "gcov"


GCC = find_gcc()
CFLAGS = ["-Wall", "-Wextra", "-DTEST"]
LDFLAGS = ["-lm"]   # libm for attitude math (atan2f/sqrtf/floorf) in host tests
INCLUDES = ["-I.", "-I../src", "-I../src/drivers", "-Iunity", "-I../config",
            "-I../../../static/stm32g0/HAL/Inc",
            "-I../../../static/stm32g0/CMSIS/Device/ST/STM32G0xx/Include",
            # 注意：vendored 的 CMSIS Core 头直接放在 Core/ 下，没有 Include/ 子目录
            # （`ls static/stm32g0/CMSIS/Core/` 可见 core_cm0plus.h）。此前这里写的是
            # Core/Include，那个目录不存在 —— 路径写错不会报错，只会在有人真的
            # include CMSIS 时以"core_cm0plus.h: No such file"的形式暴露。
            "-I../../../static/stm32g0/CMSIS/Core",
            "-I../../../static/hw2c_cli",
            "-I../../../static/third_party/lwrb"]
UNITY_SRC = "unity/unity.c"
MOCK_SRC = "mock_hal.c"
EXTRA_SRCS = []


def find_tests():
    """Return a list of test names (without .c extension) for all test_*.c files."""
    tests = []
    for f in glob.glob("test_*.c"):
        name = os.path.splitext(f)[0]
        tests.append(name)
    return sorted(tests)


def compile_test(name, coverage=False):
    src = name + ".c"
    exe = name + ".exe" if sys.platform.startswith("win") else name
    cmd = [GCC] + CFLAGS + INCLUDES + LDFLAGS + [UNITY_SRC, MOCK_SRC] + EXTRA_SRCS + [src, "-o", exe]
    if coverage:
        # 在编译和链接中启用 coverage
        cmd = [GCC, "--coverage"] + CFLAGS + INCLUDES + LDFLAGS + [UNITY_SRC, MOCK_SRC] + EXTRA_SRCS + [src, "-o", exe]
    print(f"Compiling {name}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Compilation failed for {name}:")
        print(result.stderr)
        return False
    return True


def run_test(name):
    exe = name + ".exe" if sys.platform.startswith("win") else name
    print(f"Running {name}...")
    result = subprocess.run(["." + os.sep + exe], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"{name} FAILED")
        return False
    print(f"{name} PASSED")
    return True


def generate_coverage_report():
    print("\nGenerating coverage report...")
    gcda_files = glob.glob("*.gcda")
    if not gcda_files:
        print("No coverage data found.")
        return

    gcov_path = find_gcov()

    for gcno in glob.glob("*.gcno"):
        cmd = [gcov_path, gcno]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Warning: gcov failed for {gcno}: {result.stderr}")

    print("Coverage reports generated. See *.gcov files in this directory.")


def main():
    parser = argparse.ArgumentParser(description="Run unit tests")
    parser.add_argument("--coverage", action="store_true", help="Generate coverage report")
    args = parser.parse_args()

    # change to the directory where this script resides (test/)
    os.chdir(Path(__file__).parent)

    tests = find_tests()
    if not tests:
        print("No test_*.c files found.")
        sys.exit(1)

    # 跑完全部用例再汇总失败清单。
    #
    # 早先这里是"遇到第一个失败就 sys.exit(1)"，结果是：一个工程里同时坏了
    # 三个用例时，只看这个脚本会让人以为"只有一个问题"。修掉第一个之后
    # 以为完事了，第二个才浮出来 —— 排查成本被这个"提前退出"放大了好几倍。
    results = []
    for test in tests:
        if not compile_test(test, coverage=args.coverage):
            results.append((test, "compile-failed"))
            continue
        results.append((test, "ok" if run_test(test) else "run-failed"))

    if args.coverage:
        generate_coverage_report()

    failed = [(name, status) for name, status in results if status != "ok"]
    if failed:
        print("=" * 64)
        print(f"{len(failed)}/{len(results)} 个测试未通过：")
        for name, status in failed:
            print(f"  - {name}: {status}")
        print("=" * 64)
        sys.exit(1)

    print(f"All {len(results)} tests passed.")


if __name__ == "__main__":
    main()
