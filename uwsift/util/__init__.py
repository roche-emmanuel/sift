#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
import sys

from uwsift.util.default_paths import (  # noqa
    DOCUMENT_SETTINGS_DIR,
    USER_CACHE_DIR,
    USER_DESKTOP_DIRECTORY,
    WORKSPACE_DB_DIR,
)
from uwsift.util.heap_profiler import HeapProfiler

LOG = logging.getLogger(__name__)
IS_FROZEN = getattr(sys, "frozen", False)
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def check_imageio_deps():
    if IS_FROZEN and not os.getenv("IMAGEIO_FFMPEG_EXE"):
        ffmpeg_exe = os.path.realpath(os.path.join(SCRIPT_DIR, "..", "..", "ffmpeg"))
        LOG.debug("Setting ffmpeg location to %s", ffmpeg_exe)
        os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg_exe


def check_grib_definition_dir():
    # patch GRIB API C library when frozen
    var_name = "ECCODES_DEFINITION_PATH"
    grib_paths = []
    if os.getenv(var_name):
        grib_paths.append(os.getenv(var_name))

    # Add NCEP specific definition locations
    grib_paths.append(os.path.realpath(os.path.join(get_package_data_dir(), "grib_definitions")))

    if IS_FROZEN:
        # add the ECCodes definitions because otherwise they point to the
        # wrong location
        grib_paths.append(os.path.realpath(os.path.join(SCRIPT_DIR, "..", "..", "share", "eccodes", "definitions")))
    else:
        grib_paths.append(os.path.join(prefix_share_dir(), "eccodes", "definitions"))

    if grib_paths:
        grib_var_value = ":".join(grib_paths)
        LOG.info("Setting GRIB definition path to %s", grib_var_value)
        os.environ[var_name] = grib_var_value


def get_package_data_dir():
    """Return location of the package 'data' directory.

    When frozen the data directory is placed in 'sift_data' of the root
    package directory.
    """
    if IS_FROZEN:
        return os.path.realpath(os.path.join(SCRIPT_DIR, "..", "..", "sift_data"))
    else:
        return os.path.realpath(os.path.join(SCRIPT_DIR, "..", "data"))


def prefix_share_dir():
    return os.path.realpath(os.path.join(sys.prefix, "share"))
