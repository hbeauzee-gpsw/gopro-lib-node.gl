#!/usr/bin/env python3
#
# Copyright 2021-2022 GoPro Inc.
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import argparse
import glob
import hashlib
import logging
import os
import os.path as op
import pathlib
import platform
import shlex
import shutil
import stat
import sysconfig
import tarfile
import urllib.request
import venv
import zipfile
from multiprocessing import Pool
from subprocess import run

_ROOTDIR = op.abspath(op.dirname(__file__))
_SYSTEM = "MinGW" if sysconfig.get_platform().startswith("mingw") else platform.system()
_RENDERDOC_ID = f"renderdoc_{_SYSTEM}"
_EXTERNAL_DEPS = dict(
    sxplayer=dict(
        version="9.13.0",
        url="https://github.com/Stupeflix/sxplayer/archive/v@VERSION@.tar.gz",
        dst_file="sxplayer-@VERSION@.tar.gz",
        sha256="272ecf2a31238440a8fc70dbe6431989d06cf77b9f15a26a55c6c91244768fca",
    ),
    pkgconf=dict(
        version="1.8.0",
        url="https://distfiles.dereferenced.org/pkgconf/pkgconf-@VERSION@.tar.xz",
        sha256="ef9c7e61822b7cb8356e6e9e1dca58d9556f3200d78acab35e4347e9d4c2bbaf",
    ),
    renderdoc_Windows=dict(
        version="1.18",
        url="https://renderdoc.org/stable/@VERSION@/RenderDoc_@VERSION@_64.zip",
        sha256="a97a9911850c8a93dc1dee8f94e339cd5933310513dddf0216d27cea3a5f25b1",
    ),
    renderdoc_Linux=dict(
        version="1.18",
        url="https://renderdoc.org/stable/@VERSION@/renderdoc_@VERSION@.tar.gz",
        sha256="c8ec16f7463266641e21b64f8e436a452a15105e4bd517bf114a9349d74cc02e",
    ),
)


def _get_external_deps(args):
    deps = ["sxplayer"]
    if _SYSTEM == "Windows":
        deps.append("pkgconf")
    if "gpu_capture" in args.debug_opts:
        if _SYSTEM not in {"Windows", "Linux"}:
            raise Exception(f"Renderdoc is not supported on {_SYSTEM}")
        deps.append(_RENDERDOC_ID)
    return {dep: _EXTERNAL_DEPS[dep] for dep in deps}


def _guess_base_dir(dirs):
    smallest_dir = sorted(dirs, key=lambda x: len(x))[0]
    return pathlib.Path(smallest_dir).parts[0]


def _get_brew_prefix():
    prefix = None
    try:
        proc = run(["brew", "--prefix"], capture_output=True, text=True, check=True)
        prefix = proc.stdout.strip()
    except FileNotFoundError:
        # Silently pass if brew is not installed
        pass
    return prefix


def _file_chk(path, chksum_hexdigest):
    chksum = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(8196)
            if not buf:
                break
            chksum.update(buf)
    match_ = chksum.hexdigest() == chksum_hexdigest
    if not match_:
        logging.warning("%s: mismatching check sum", path)
    return match_


def _fix_permissions(path):
    for root, dirs, files in os.walk(path, topdown=True):
        for file in files:
            os.chmod(op.join(root, file), stat.S_IRUSR | stat.S_IWUSR)
        for directory in dirs:
            os.chmod(op.join(root, directory), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _rmtree(path, ignore_errors=False, onerror=None):
    """
    shutil.rmtree wrapper that is resilient to permission issues when
    encountering read-only files or directories lacking the executable
    permission.
    """
    try:
        shutil.rmtree(path, ignore_errors, onerror=onerror)
    except Exception:
        _fix_permissions(path)
        shutil.rmtree(path, ignore_errors, onerror=onerror)


def _download_extract(dep_item):
    logging.basicConfig(level="INFO")  # Needed for every process on Windows

    name, dep = dep_item

    version = dep["version"]
    url = dep["url"].replace("@VERSION@", version)
    chksum = dep["sha256"]
    dst_file = dep.get("dst_file", op.basename(url)).replace("@VERSION@", version)
    dst_base = op.join(_ROOTDIR, "external")
    dst_path = op.join(dst_base, dst_file)
    os.makedirs(dst_base, exist_ok=True)

    # Download
    if not op.exists(dst_path) or not _file_chk(dst_path, chksum):
        logging.info("downloading %s to %s", url, dst_file)
        urllib.request.urlretrieve(url, dst_path)
        assert _file_chk(dst_path, chksum)

    # Extract
    if tarfile.is_tarfile(dst_path):
        with tarfile.open(dst_path) as tar:
            dirs = {f.name for f in tar.getmembers() if f.isdir()}
            extract_dir = op.join(dst_base, _guess_base_dir(dirs))
            if not op.exists(extract_dir):
                logging.info("extracting %s", dst_file)
                tar.extractall(dst_base)

    elif zipfile.is_zipfile(dst_path):
        with zipfile.ZipFile(dst_path) as zip_:
            dirs = {op.dirname(f) for f in zip_.namelist()}
            extract_dir = op.join(dst_base, _guess_base_dir(dirs))
            if not op.exists(extract_dir):
                logging.info("extracting %s", dst_file)
                zip_.extractall(dst_base)
    else:
        assert False

    # Remove previous link if needed
    target = op.join(dst_base, name)
    rel_extract_dir = op.basename(extract_dir)
    if op.islink(target) and os.readlink(target) != rel_extract_dir:
        logging.info("unlink %s target", target)
        os.unlink(target)
    elif op.exists(target) and not op.islink(target):
        logging.info("remove previous %s copy", target)
        _rmtree(target)

    # Link (or copy)
    if not op.exists(target):
        logging.info("symlink %s -> %s", target, rel_extract_dir)
        try:
            os.symlink(rel_extract_dir, target)
        except OSError:
            # This typically happens on Windows when Developer Mode is not
            # available/enabled
            logging.info("unable to symlink, fallback on copy (%s -> %s)", extract_dir, target)
            shutil.copytree(extract_dir, target)

    return name, target


def _fetch_externals(args):
    dependencies = _get_external_deps(args)
    with Pool() as p:
        return dict(p.map(_download_extract, dependencies.items()))


def _block(name, prerequisites=None):
    def real_decorator(block_func):
        block_func.name = name
        block_func.prerequisites = prerequisites if prerequisites else []
        return block_func

    return real_decorator


def _meson_compile_install_cmd(component, external=False):
    builddir = op.join("external", component, "builddir") if external else op.join("builddir", component)
    return ["$(MESON) " + _cmd_join(action, "-C", builddir) for action in ("compile", "install")]


@_block("pkgconf-setup")
def _pkgconf_setup(cfg):
    builddir = op.join("external", "pkgconf", "builddir")
    return ["$(MESON_SETUP) " + _cmd_join("-Dtests=false", cfg.externals["pkgconf"], builddir)]


@_block("pkgconf-install", [_pkgconf_setup])
def _pkgconf_install(cfg):
    ret = _meson_compile_install_cmd("pkgconf", external=True)
    pkgconf_exe = op.join(cfg.bin_path, "pkgconf.exe")
    pkgconfig_exe = op.join(cfg.bin_path, "pkg-config.exe")
    return ret + [f"copy {pkgconf_exe} {pkgconfig_exe}"]


@_block("sxplayer-setup")
def _sxplayer_setup(cfg):
    builddir = op.join("external", "sxplayer", "builddir")
    return ["$(MESON_SETUP) -Drpath=true " + _cmd_join(cfg.externals["sxplayer"], builddir)]


@_block("sxplayer-install", [_sxplayer_setup])
def _sxplayer_install(cfg):
    return _meson_compile_install_cmd("sxplayer", external=True)


@_block("renderdoc-install")
def _renderdoc_install(cfg):
    renderdoc_dll = op.join(cfg.externals[_RENDERDOC_ID], "renderdoc.dll")
    return [f"copy {renderdoc_dll} {cfg.bin_path}"]


@_block("nodegl-setup", [_sxplayer_install])
def _nodegl_setup(cfg):
    nodegl_opts = []
    if cfg.args.debug_opts:
        debug_opts = ",".join(cfg.args.debug_opts)
        nodegl_opts += [f"-Ddebug_opts={debug_opts}"]

    if "gpu_capture" in cfg.args.debug_opts:
        renderdoc_dir = cfg.externals[_RENDERDOC_ID]
        nodegl_opts += [f"-Drenderdoc_dir={renderdoc_dir}"]

    extra_library_dirs = []
    extra_include_dirs = []
    if _SYSTEM == "Windows":
        vcpkg_prefix = op.join(cfg.args.vcpkg_dir, "installed", "x64-windows")
        extra_library_dirs += [
            op.join(cfg.prefix, "Lib"),
            op.join(vcpkg_prefix, "lib"),
        ]
        extra_include_dirs += [
            op.join(cfg.prefix, "Include"),
            op.join(vcpkg_prefix, "include"),
        ]
    elif _SYSTEM == "Darwin":
        prefix = _get_brew_prefix()
        if prefix:
            extra_library_dirs += [op.join(prefix, "lib")]
            extra_include_dirs += [op.join(prefix, "include")]

    if extra_library_dirs:
        opts = ",".join(extra_library_dirs)
        nodegl_opts += [f"-Dextra_library_dirs={opts}"]

    if extra_include_dirs:
        opts = ",".join(extra_include_dirs)
        nodegl_opts += [f"-Dextra_include_dirs={opts}"]

    return ["$(MESON_SETUP) -Drpath=true " + _cmd_join(*nodegl_opts, "libnodegl", op.join("builddir", "libnodegl"))]


@_block("nodegl-install", [_nodegl_setup])
def _nodegl_install(cfg):
    return _meson_compile_install_cmd("libnodegl")


@_block("pynodegl-deps-install", [_nodegl_install])
def _pynodegl_deps_install(cfg):
    return ["$(PIP) " + _cmd_join("install", "-r", op.join(".", "pynodegl", "requirements.txt"))]


@_block("pynodegl-install", [_pynodegl_deps_install])
def _pynodegl_install(cfg):
    ret = ["$(PIP) " + _cmd_join("-v", "install", "-e", op.join(".", "pynodegl"))]
    if _SYSTEM == "Windows":
        dlls = op.join(cfg.prefix, "Scripts", "*.dll")
        ret += [f"xcopy /Y {dlls} pynodegl\\."]
    else:
        rpath = op.join(cfg.prefix, "lib")
        ldflags = f"-Wl,-rpath,{rpath}"
        ret[0] = f"LDFLAGS={ldflags} {ret[0]}"
    return ret


@_block("pynodegl-utils-deps-install", [_pynodegl_install])
def _pynodegl_utils_deps_install(cfg):
    #
    # Requirements not installed on MinGW because:
    # - PySide6 can't be pulled (required to be installed by the user outside the
    #   Python virtual env)
    # - Pillow fails to find zlib (required to be installed by the user outside the
    #   Python virtual env)
    #
    if _SYSTEM == "MinGW":
        return ["@"]  # noop
    return ["$(PIP) " + _cmd_join("install", "-r", op.join(".", "pynodegl-utils", "requirements.txt"))]


@_block("pynodegl-utils-install", [_pynodegl_utils_deps_install])
def _pynodegl_utils_install(cfg):
    return ["$(PIP) " + _cmd_join("-v", "install", "-e", op.join(".", "pynodegl-utils"))]


@_block("ngl-tools-setup", [_nodegl_install])
def _ngl_tools_setup(cfg):
    return ["$(MESON_SETUP) -Drpath=true " + _cmd_join("ngl-tools", op.join("builddir", "ngl-tools"))]


@_block("ngl-tools-install", [_ngl_tools_setup])
def _ngl_tools_install(cfg):
    return _meson_compile_install_cmd("ngl-tools")


def _nodegl_run_target_cmd(cfg, target):
    builddir = op.join("builddir", "libnodegl")
    return ["$(MESON) " + _cmd_join("compile", "-C", builddir, target)]


@_block("nodegl-updatedoc", [_nodegl_install])
def _nodegl_updatedoc(cfg):
    return _nodegl_run_target_cmd(cfg, "updatedoc")


@_block("nodegl-updatespecs", [_nodegl_install])
def _nodegl_updatespecs(cfg):
    return _nodegl_run_target_cmd(cfg, "updatespecs")


@_block("nodegl-updateglwrappers", [_nodegl_install])
def _nodegl_updateglwrappers(cfg):
    return _nodegl_run_target_cmd(cfg, "updateglwrappers")


@_block("all", [_ngl_tools_install, _pynodegl_utils_install])
def _all(cfg):
    echo = ["", "Build completed.", "", "You can now enter the venv with:"]
    if _SYSTEM == "Windows":
        echo.append(op.join(cfg.bin_path, "Activate.ps1"))
        return [f"@echo.{e}" for e in echo]
    else:
        echo.append(" " * 4 + ". " + op.join(cfg.bin_path, "activate"))
        return [f'@echo "    {e}"' for e in echo]


@_block("tests-setup", [_ngl_tools_install, _pynodegl_utils_install])
def _tests_setup(cfg):
    return ["$(MESON_SETUP_TESTS) " + _cmd_join("tests", op.join("builddir", "tests"))]


@_block("nodegl-tests", [_nodegl_install])
def _nodegl_tests(cfg):
    return ["$(MESON) " + _cmd_join("test", "-C", op.join("builddir", "libnodegl"))]


def _rm(f):
    return f"(if exist {f} del /q {f})" if _SYSTEM == "Windows" else f"$(RM) {f}"


def _rd(d):
    return f"(if exist {d} rd /s /q {d})" if _SYSTEM == "Windows" else f"$(RM) -r {d}"


@_block("clean-py")
def _clean_py(cfg):
    return [
        _rm(op.join("pynodegl", "nodes_def.pyx")),
        _rm(op.join("pynodegl", "_pynodegl.c")),
        _rm(op.join("pynodegl", "_pynodegl.*.so")),
        _rm(op.join("pynodegl", "pynodegl.*.pyd")),
        _rm(op.join("pynodegl", "pynodegl/__init__.py")),
        _rd(op.join("pynodegl", "build")),
        _rd(op.join("pynodegl", "pynodegl.egg-info")),
        _rd(op.join("pynodegl", ".eggs")),
        _rd(op.join("pynodegl-utils", "pynodegl_utils.egg-info")),
        _rd(op.join("pynodegl-utils", ".eggs")),
    ]


@_block("clean", [_clean_py])
def _clean(cfg):
    return [
        _rd(op.join("builddir", "libnodegl")),
        _rd(op.join("builddir", "ngl-tools")),
        _rd(op.join("builddir", "tests")),
        _rd(op.join("external", "pkgconf", "builddir")),
        _rd(op.join("external", "sxplayer", "builddir")),
    ]


def _coverage(cfg, output):
    # We don't use `meson coverage` here because of
    # https://github.com/mesonbuild/meson/issues/7895
    return [_cmd_join("ninja", "-C", op.join("builddir", "libnodegl"), f"coverage-{output}")]


@_block("coverage-html")
def _coverage_html(cfg):
    return _coverage(cfg, "html")


@_block("coverage-xml")
def _coverage_xml(cfg):
    return _coverage(cfg, "xml")


@_block("tests", [_nodegl_tests, _tests_setup])
def _tests(cfg):
    return ["$(MESON) " + _cmd_join("test", "-C", op.join("builddir", "tests"))]


def _quote(s):
    if not s or " " in s:
        return f'"{s}"'
    assert "'" not in s
    assert '"' not in s
    return s


def _cmd_join(*cmds):
    if _SYSTEM == "Windows":
        return " ".join(_quote(cmd) for cmd in cmds)
    return shlex.join(cmds)


def _get_make_vars(cfg):
    debug = cfg.args.coverage or cfg.args.buildtype == "debug"

    # We don't want Python to fallback on one found in the PATH so we explicit
    # it to the one in the venv.
    python = op.join(cfg.bin_path, "python")

    #
    # MAKEFLAGS= is a workaround (not working on Windows due to incompatible Make
    # syntax) for the issue described here:
    # https://github.com/ninja-build/ninja/issues/1139#issuecomment-724061270
    #
    # Note: this will invoke the meson in the venv, unless we're on MinGW where
    # it will fallback on the system one. This is due to the extended PATH
    # mechanism.
    #
    meson = "MAKEFLAGS= meson" if _SYSTEM != "Windows" else "meson"

    meson_setup = [
        "setup",
        "--prefix",
        cfg.prefix,
        "--pkg-config-path",
        cfg.pkg_config_path,
        "--buildtype",
        "debugoptimized" if debug else "release",
    ]
    if cfg.args.coverage:
        meson_setup += ["-Db_coverage=true"]
    if _SYSTEM != "MinGW" and "debug" not in cfg.args.buildtype:
        meson_setup += ["-Db_lto=true"]

    if _SYSTEM == "Windows":
        meson_setup += ["--bindir=Scripts", "--libdir=Lib", "--includedir=Include"]
    elif op.isfile("/etc/debian_version"):
        # Workaround Debian/Ubuntu bug; see https://github.com/mesonbuild/meson/issues/5925
        meson_setup += ["--libdir=lib"]

    ret = dict(
        PIP=_cmd_join(python, "-m", "pip"),
        MESON=meson,
    )

    ret["MESON_SETUP"] = "$(MESON) " + _cmd_join(*meson_setup, f"--backend={cfg.args.build_backend}")
    # Our tests/meson.build logic is not well supported with the VS backend so
    # we need to fallback on Ninja
    ret["MESON_SETUP_TESTS"] = "$(MESON) " + _cmd_join(*meson_setup, "--backend=ninja")

    return ret


def _get_makefile_rec(cfg, blocks, declared):
    ret = ""
    for block in blocks:
        if block.name in declared:
            continue
        declared |= {block.name}
        req_names = " ".join(r.name for r in block.prerequisites)
        req = f" {req_names}" if req_names else ""
        commands = "\n".join("\t" + cmd for cmd in block(cfg))
        ret += f"{block.name}:{req}\n{commands}\n"
        ret += _get_makefile_rec(cfg, block.prerequisites, declared)
    return ret


def _get_makefile(cfg, blocks):
    env = cfg.get_env()
    env_vars = {k: _quote(v) for k, v in env.items()}
    if _SYSTEM == "Windows":
        #
        # Environment variables are altered if and only if they already exists
        # in the environment. While this is (usually) true for PATH, it isn't
        # for the others we're trying to declare. This "if [set...]" trick is
        # to circumvent this issue.
        #
        # See https://stackoverflow.com/questions/38381422/how-do-i-set-an-environment-variables-with-nmake
        #
        vars_export = "\n".join(f"{k} = {v}" for k, v in env_vars.items()) + "\n"
        vars_export_cond = " || ".join(f"[set {k}=$({k})]" for k in env_vars.keys())
        vars_export += f"!if {vars_export_cond}\n!endif\n"
    else:
        # We must immediate assign with ":=" since sometimes values contain
        # reference to themselves (typically PATH)
        vars_export = "\n".join(f"export {k} := {v}" for k, v in env_vars.items()) + "\n"

    make_vars = _get_make_vars(cfg)
    ret = "\n".join(f"{k} = {v}" for k, v in make_vars.items()) + "\n" + vars_export

    declared = set()
    ret += _get_makefile_rec(cfg, blocks, declared)
    ret += ".PHONY: " + " ".join(declared) + "\n"
    return ret


class _EnvBuilder(venv.EnvBuilder):
    def __init__(self):
        super().__init__(system_site_packages=_SYSTEM == "MinGW", with_pip=True, prompt="nodegl")

    def post_setup(self, context):
        if _SYSTEM == "MinGW":
            return
        pip_install = [context.env_exe, "-m", "pip", "install"]
        pip_install += ["meson", "ninja"]
        logging.info("install build dependencies: %s", _cmd_join(*pip_install))
        run(pip_install, check=True)


def _build_venv(args):
    if op.exists(args.venv_path):
        logging.warning("Python virtual env already exists at %s", args.venv_path)
        return
    logging.info("creating Python virtualenv: %s", args.venv_path)
    _EnvBuilder().create(args.venv_path)


class _Config:
    def __init__(self, args):
        self.args = args
        self.prefix = op.abspath(args.venv_path)

        # On MinGW we need the path translated from C:\ to /c/, because when
        # part of the PATH, the ':' separator will break
        if _SYSTEM == "MinGW":
            self.prefix = run(["cygpath", "-u", self.prefix], capture_output=True, text=True).stdout.strip()

        self.bin_name = "Scripts" if _SYSTEM == "Windows" else "bin"
        self.bin_path = op.join(self.prefix, self.bin_name)
        if _SYSTEM == "Windows":
            vcpkg_pc_path = op.join(args.vcpkg_dir, "installed", "x64-windows", "lib", "pkgconfig")
            self.pkg_config_path = os.pathsep.join((vcpkg_pc_path, op.join(self.prefix, "Lib", "pkgconfig")))
        else:
            self.pkg_config_path = op.join(self.prefix, "lib", "pkgconfig")
        self.externals = _fetch_externals(args)

        if _SYSTEM == "Windows":
            _sxplayer_setup.prerequisites.append(_pkgconf_install)
            if "gpu_capture" in args.debug_opts:
                _nodegl_setup.prerequisites.append(_renderdoc_install)

            vcpkg_bin = op.join(args.vcpkg_dir, "installed", "x64-windows", "bin")
            for f in glob.glob(op.join(vcpkg_bin, "*.dll")):
                logging.info("copy %s to venv/Scripts", f)
                shutil.copy2(f, op.join("venv", "Scripts"))

    def get_env(self):
        sep = ":" if _SYSTEM == "MinGW" else os.pathsep
        env = {}
        env["PATH"] = sep.join((self.bin_path, "$(PATH)"))
        env["PKG_CONFIG_PATH"] = self.pkg_config_path
        if _SYSTEM == "Windows":
            env["PKG_CONFIG_ALLOW_SYSTEM_LIBS"] = "1"
            env["PKG_CONFIG_ALLOW_SYSTEM_CFLAGS"] = "1"
        elif _SYSTEM == "MinGW":
            # See https://setuptools.pypa.io/en/latest/deprecated/distutils-legacy.html
            env["SETUPTOOLS_USE_DISTUTILS"] = "stdlib"
        return env


def _run():
    default_build_backend = "ninja" if _SYSTEM != "Windows" else "vs"
    parser = argparse.ArgumentParser(
        description="Create and manage a standalone node.gl virtual environement",
    )
    parser.add_argument("-p", "--venv-path", default=op.join(_ROOTDIR, "venv"), help="Virtual environment directory")
    parser.add_argument("--buildtype", choices=("release", "debug"), default="release", help="Build type")
    parser.add_argument("--coverage", action="store_true", help="Code coverage")
    parser.add_argument(
        "-d",
        "--debug-opts",
        nargs="+",
        default=[],
        choices=("gl", "vk", "mem", "scene", "gpu_capture"),
        help="Debug options",
    )
    parser.add_argument(
        "--build-backend", choices=("ninja", "vs"), default=default_build_backend, help="Build backend to use"
    )
    if _SYSTEM == "Windows":
        parser.add_argument("--vcpkg-dir", default=r"C:\vcpkg", help="Vcpkg directory")

    args = parser.parse_args()

    logging.basicConfig(level="INFO")

    _build_venv(args)

    dst_makefile = op.join(_ROOTDIR, "Makefile")
    logging.info("writing %s", dst_makefile)
    cfg = _Config(args)
    blocks = [
        _all,
        _tests,
        _clean,
        _nodegl_updatedoc,
        _nodegl_updatespecs,
        _nodegl_updateglwrappers,
    ]
    if args.coverage:
        blocks += [_coverage_html, _coverage_xml]
    makefile = _get_makefile(cfg, blocks)
    with open(dst_makefile, "w") as f:
        f.write(makefile)


if __name__ == "__main__":
    _run()
