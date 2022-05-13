#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
uwsift.model.document
---------------------

Core (low-level) document model for SIFT.
The core is sometimes accessed via Facets, which are like database views for a specific group of use cases

The document model is a metadata representation which permits the workspace to be constructed and managed.

Document is primarily a composition of layers.
Layers come in several flavors:

 - Image : a float32 field shown as tiles containing strides and/or alternate LODs, having a colormap
 - Outline : typically a geographic political map
 - Shape : a highlighted region selected by the user: point, line (great circle), polygon
 - Combination : calculated from two or more image layers, e.g. RGBA combination of images
                 combinations may be limited to areas specified by region layers.

Future Work:

 - Volume : a 3D dataset, potentially sparse
 - DenseVolume
 - SparseVolume : (x,y,z) point cloud

Layers are represented in 1 or more LayerSets, which are alternate configurations of the display.
Users may wish to display the same data in several different ways for illustration purposes.
Only one LayerSet is used on a given Map window at a time.

Layers have presentation settings that vary with LayerSet:

 - z_order: bottom to top in the map display
 - visible: whether or not it's being drawn on the map
 - a_order: animation order, when the animation button is hit
 - colormap: how the data is converted to pixels
 - mixing: mixing mode when drawing (normal, additive)

Document has zero or more Probes. Layers also come in multiple
flavors that may be be attached to plugins or helper applications.

 - Scatter: (layerA, layerB, region) -> xy plot
 - Slice: (volume, line) -> curtain plot
 - Profile: (volume, point) -> profile plot

Document has zero or more Colormaps, determining how they're presented

The document does not own data (content). It only owns metadata (info).
At most, document holds coarse overview data content for preview purposes.

All entities in the Document have a UUID that is their identity throughout their lifecycle,
and is often used as shorthand between subsystems. Document rarely deals directly with content.

:author: R.K.Garcia <rayg@ssec.wisc.edu>
:copyright: 2015 by University of Wisconsin Regents, see AUTHORS for more details
:license: GPLv3, see LICENSE for more details
"""
from __future__ import annotations

__author__ = "rayg"
__docformat__ = "reStructuredText"

import dataclasses
import json
import logging
import os
import typing as typ
import warnings
from collections import MutableSequence, OrderedDict, defaultdict
from datetime import datetime, timedelta
from itertools import chain, groupby
from uuid import UUID
from uuid import uuid1 as uuidgen
from weakref import ref

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from uwsift.common import FCS_SEP, Flags, Info, Kind, Presentation, Span, ZList
from uwsift.model.area_definitions_manager import AreaDefinitionsManager
from uwsift.model.layer import (
    DocBasicDataset,
    DocCompositeDataset,
    DocDataset,
    DocRGBDataset,
    Mixing,
)
from uwsift.queue import TASK_DOING, TASK_PROGRESS, TaskQueue
from uwsift.util.common import units_conversion
from uwsift.util.default_paths import DOCUMENT_SETTINGS_DIR
from uwsift.view.colormap import (
    COLORMAP_MANAGER,
    SITE_CATEGORY,
    USER_CATEGORY,
    PyQtGraphColormap,
)
from uwsift.workspace import BaseWorkspace, CachingWorkspace, SimpleWorkspace
from uwsift.workspace.metadatabase import Product
from uwsift.workspace.workspace import frozendict

LOG = logging.getLogger(__name__)

DEFAULT_LAYER_SET_COUNT = 1  # this should match the ui configuration!


class DocLayerStack(MutableSequence):
    """list-like layer set which will slowly eat functionality from Document as warranted

    Provide cleaner interfacing to GUI elements.

    """

    _doc = None  # weakref to document we belong to
    _store = None
    _u2r = None  # uuid-to-row correspondence cache

    def __init__(self, doc, *args, **kwargs):
        if isinstance(doc, DocLayerStack):
            self._doc = ref(doc._doc())
            self._store = list(doc._store)
        elif isinstance(doc, Document):
            self._doc = ref(doc)
            self._store = list(*args)
        else:
            raise ValueError("cannot initialize DocLayerStack using %s" % type(doc))

    def __setitem__(self, index: int, value: Presentation):
        if index >= 0 and index < len(self._store):
            self._store[index] = value
        elif index == len(self._store):
            self._store.append(value)
        else:
            raise IndexError("%d not a valid index" % index)
        self._u2r = None

    @property
    def uuid2row(self):
        if self._u2r is None:
            self._u2r = dict((p.uuid, i) for (i, p) in enumerate(self._store))
        return self._u2r

    def __getitem__(self, index: int):  # then return layer object
        if isinstance(index, int):
            return self._store[index]
        elif isinstance(index, UUID):  # then return 0..n-1 index in stack
            return self.uuid2row.get(index, None)
        elif isinstance(index, DocDataset):
            return self.uuid2row.get(index.uuid, None)
        elif isinstance(index, Presentation):
            return self.uuid2row.get(index.uuid, None)
        else:
            raise ValueError("unable to index LayerStack using %s" % repr(index))

    def __iter__(self):
        for each in self._store:
            yield each

    def __len__(self):
        return len(self._store)

    def __delitem__(self, index: int):
        del self._store[index]
        self._u2r = None

    def insert(self, index: int, value: Presentation):
        self._store.insert(index, value)
        self._u2r = None

    def clear_animation_order(self):
        for i, q in enumerate(self._store):
            self._store[i] = dataclasses.replace(q, a_order=None)

    def index(self, uuid):
        assert isinstance(uuid, UUID)
        u2r = self.uuid2row
        return u2r.get(uuid, None)

    def change_order_by_indices(self, new_order):
        self._u2r = None
        revised = [self._store[n] for n in new_order]
        self._store = revised

    @property
    def animation_order(self):
        aouu = [(x.a_order, x.uuid) for x in self._store if (x.a_order is not None)]
        aouu.sort()
        ao = tuple(u for a, u in aouu)
        LOG.debug("animation order is {0!r:s}".format(ao))
        return ao

    @animation_order.setter
    def animation_order(self, layer_or_uuid_seq):
        self.clear_animation_order()
        for nth, lu in enumerate(layer_or_uuid_seq):
            try:
                idx = self[lu]
            except ValueError:
                LOG.warning("unable to find layer in LayerStack")
                raise
            self._store[idx] = dataclasses.replace(self._store[idx], a_order=nth)


# FUTURE: move these into separate modules


class DocumentAsContextBase(object):
    """Base class for high-level document context objects
    When used as a context object, defers modifications and signals until exit!
    """

    doc = None
    mdb = None
    ws = None
    _finally_queue = None

    def __init__(self, doc, mdb, ws):
        self.doc = doc
        self.mdb = mdb
        self.ws = ws

    def _finally(self, fn: typ.Callable, *args, **kwargs):
        """Defer a call until context close, if a context is active; else do immediately"""
        from functools import partial

        if args or kwargs:
            fn = partial(fn, *args, **kwargs)
        if self._finally_queue is not None:
            self._finally_queue.append(fn)
        else:
            fn()

    def __enter__(self):
        self._finally_queue = []

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            raise exc_val
        else:
            # commit code
            elfin, self._finally_queue = self._finally_queue, None
            while True:
                todo = elfin.pop(0, None)
                if todo:
                    todo()
                else:
                    break


# class DocumentAsContext(DocumentAsContextBase):
#     def __enter__(self):
#         raise NotImplementedError()
#
#     def __exit__(self, exc_type, exc_val, exc_tb):
#         if exc_val is not None:
#             # abort code
#             pass
#         else:
#             # commit code
#             pass
#         raise NotImplementedError()


###################################################################################################################


class LayerInfo(typ.NamedTuple):
    uuid: UUID
    time_label: str
    presentation: Presentation
    f_convert: typ.Callable
    f_format: typ.Callable

    @property
    def colormap(self):
        return self.presentation.colormap

    @property
    def climits(self):
        return self.presentation.climits


class DocumentAsLayerStack(DocumentAsContextBase):
    """Represent the document as a list of current layers
    As we transition to timeline model, this stops representing products and starts being a track stack
    """

    # whether to represent all active products in document timespan, or just the ones under the playhead
    _all_active: bool = False

    def __enter__(self):
        raise NotImplementedError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            pass
        else:
            # commit code
            pass
        raise NotImplementedError()

    @property
    def current_layer_set(self) -> DocLayerStack:
        warnings.warn("this compatibility interface changes for full timeline model", DeprecationWarning)
        return self.doc.current_layer_set

    def uuid_for_current_layer(self, layer_index: int) -> UUID:
        """given layer index from 0==top_z, return UUID of product"""
        warnings.warn("this compatibility interface is currently not implemented", DeprecationWarning)
        return self.doc.uuid_for_current_layer(layer_index)

    def _layer_info_for_uuid(self, uuid) -> LayerInfo:
        with self.mdb as s:
            prod = s.query(Product).filter_by(uuid_str=str(uuid)).first()
            nfo = prod.info
            return LayerInfo(
                uuid=uuid,
                time_label=nfo.get(Info.DISPLAY_TIME, "--:--"),
                presentation=self.doc.family_presentation[prod.family],
                f_convert=nfo.get(Info.UNIT_CONVERSION)[1],
                f_format=nfo.get(Info.UNIT_CONVERSION)[2],
            )

    @property
    def current_layer_uuid_order(self):
        """the current active products in top-to-bottom order"""
        if self._all_active:
            # yield all active products in track order, but decline to rearrange
            raise NotImplementedError()
        else:
            # yield the active products under the current playhead
            raise NotImplementedError()

    def prez_for_uuid(self, uuid: UUID) -> Presentation:
        """presentation settings for the product uuid"""
        return self.doc.family_presentation[self.doc.family_for_product_or_layer(uuid)]

    def time_label_for_uuid(self, uuid):
        """used to update animation display when a new frame is shown"""
        if not uuid:
            return "YYYY-MM-DD HH:MM"
        return self._layer_info_for_uuid(uuid).time_label

    def prez_for_uuids(self, uuids, lset=None):
        for uuid in uuids:
            yield self.prez_for_uuid(uuid)
        # if lset is None:
        #     lset = self.current_layer_set
        # for p in lset:
        #     if p.uuid in uuids:
        #         yield p

    def colormap_for_uuids(self, uuids, lset=None):
        for p in self.prez_for_uuids(uuids, lset=lset):
            yield p.colormap

    def colormap_for_uuid(self, uuid, lset=None):
        for p in self.colormap_for_uuids((uuid,), lset=lset):
            return p

    def valid_range_for_uuid(self, uuid):
        # Limit ourselves to what information
        # in the future valid range may be different than the default CLIMs
        return self._layer_info_for_uuid(uuid).climits
        # return self[uuid][Info.CLIM]

    def convert_value(self, uuid, x, inverse=False):
        return self._layer_info_for_uuid(uuid).f_convert(x, inverse=inverse)
        # return self[uuid][Info.UNIT_CONVERSION][1](x, inverse=inverse)

    def format_value(self, uuid, x, numeric=True, units=True):
        return self._layer_info_for_uuid(uuid).f_format(x, numeric=numeric, units=units)
        # return self[uuid][Info.UNIT_CONVERSION][2](x, numeric=numeric, units=units)

    def __len__(self):
        """Return active track count"""
        return self.doc.track_order.top_z + 1

    def _product_info_under_playhead_for_family(self, family: str) -> typ.Mapping:
        """Return the product info dictionary for"""
        raise NotImplementedError("need to consult mdb to get product info dictionary under playhead")

    def get_info(self, dex: typ.Union[int, UUID]):
        """return info dictionary with top z-order at 0, going downward"""
        if isinstance(dex, UUID):
            warnings.warn("DocumentAsLayerStack.get_info should not be accepting UUIDs", DeprecationWarning)
            with self.mdb as S:
                prod = S.query(Product).filter_by(uuid_str=str(dex)).first()
                return prod.info
        z = self.doc.track_order.top_z - dex
        fam = self.doc.track_order[z]
        prod_info = self._product_info_under_playhead_for_family(fam)
        return prod_info

    def __getitem__(self, uuid: UUID):
        if not isinstance(uuid, UUID):
            raise ValueError("need a UUID here")
        with self.mdb as S:
            prod = S.query(Product).filter_by(uuid_str=str(uuid)).first()
            return prod.info

    # FUTURE: allow re-ordering of inactive tracks?

    def reorder_by_indices(self, order: typ.Iterable[int]):
        """Re-order active (z>=0) document families with new order
        input order is expected to be listbox-like indices, i.e. order 0 is topmost z
        """
        order = list(order)
        assert len(order) == len(self)  # specified all active families
        lut = self.doc.track_order.to_dict()
        topz = self.doc.track_order.top_z
        zs = [topz - x for x in order]
        vals = [lut[z] for z in zs]
        new_zs = [topz - x for x in range(len(order))]
        self.doc.track_order.merge_subst(zip(new_zs, vals))
        self.doc.didReorderTracks.emit(set(), set())


###################################################################################################################


class FrameInfo(typ.NamedTuple):
    """Represent a data Product as information to display as a frame on timeline"""

    uuid: UUID
    ident: str  # family::category::serial
    when: Span  # time and duration of this frame
    state: Flags  # logical state for timeline to display with color and glyphs
    primary: str  # primary description for timeline, e.g. "G16 ABI B06"
    secondary: str  # secondary description, typically time information
    # thumb: QImage  # thumbnail image to embed in timeline item


class TrackInfo(typ.NamedTuple):
    track: str  # family::category
    presentation: Presentation  # colorbar, ranges, gammas, etc
    when: Span  # available time-Span of the data
    state: Flags  # any status or special Flags set on the track, according to document / workspace
    primary: str  # primary label for UI
    secondary: str  # secondary label
    frames: typ.List[FrameInfo]  # list of frames within specified time Span


class DocumentAsTrackStack(DocumentAsContextBase):
    """Work with document as tracks, named by family::category, e.g. IMAGE:geo:toa_reflectance:0.47µm::GOES-16:ABI:CONUS
    This is primarily used by timeline QGraphicsScene bridge, which displays metadatabase + document + workspace content
    zorder >=0 implies active track, i.e. one user has selected as participating in this document
    zorder <0 implies available but inactive track, i.e. metadata / resource / content are unrealized currently
    """

    _actions: typ.List[typ.Callable] = None  # only available when used as a context manager
    animating: bool = False

    @property
    def playhead_time(self) -> typ.Optional[datetime]:
        """current document playhead time, or None if animating"""
        return self.doc.playhead_time if not self.animating else None

    @playhead_time.setter
    def playhead_time(self, t: datetime):
        """Update the document's playhead time and trigger any necessary signals"""
        self.doc.playhead_time = t

    @property
    def playback_span(self) -> Span:
        pbs = self.doc.playback_span
        if not pbs or pbs.is_instantaneous:
            return self.timeline_span

    @playback_span.setter
    def playback_span(self, when: Span):
        self.doc.playback_span = when
        # FIXME: signal

    @property
    def timeline_span(self) -> Span:
        """Document preferred time-Span (user-specified or from a default)
        :return:
        """
        dts = self.doc.timeline_span  # first, check for user intent
        if not dts or dts.is_instantaneous:
            LOG.debug("document timeline Span is not set, using metadata extents")
            dts = self.doc.potential_product_span()
        if not dts or dts.is_instantaneous:
            LOG.debug("insufficient metadata, using 12h around current time due to no available timespan")
            sh = timedelta(hours=6)
            dts = Span(datetime.utcnow() - sh, sh * 2)
        return dts

    @property
    def top_z(self) -> int:
        return self.doc.track_order.top_z

    # def track_order_at_time(self, when:datetime = None,
    #                         only_active=False,
    #                         include_products=True
    #                         ) -> T.Iterable[T.Tuple[int, str, T.Optional[Product]]]:
    #     """List of tracks from highest Z order to lowest, at a given time;
    #     (zorder, track) pairs are returned, with zorder>=0 being "part of the document" and
    #     <0 being "available but nothing activated"
    #     include_products implies (zorder, track, product-or-None) tuples be yielded;
    #     otherwise tracks without products are not yielded
    #     Inactive tracks can be filtered by stopping iteration when zorder<0
    #     tracks are returned as track-name strings
    #     Product instances are readonly metadatabase entries
    #     defaults to current document time
    #     """
    #     if when is None:
    #         when = self.doc.playhead_time
    #     if when is None:
    #         raise RuntimeError("unknown time for iterating track order, animation likely in progress")
    #     with self.mdb as s:
    #         que = s.query(Product).filter((Product.obs_time < when) and (
    #                                       (Product.obs_time + Product.obs_duration) <= when))
    #         if only_active:
    #             famtab = dict((track, z) for (z, track) in self.doc.track_order.enumerate() if z>=0)
    #             active_families = set(famtab.keys())
    #             que = s.filter((Product.family + FCS_SEP + Product.category) in active_families)
    #         else:
    #             famtab = dict((track, z) for (z, track) in self.doc.track_order.enumerate())
    #         prods = list(que.all())
    #         # sort into z order according to document
    #         if include_products:
    #             zult = [(famtab[p.track], p.track, p) for p in prods]
    #         else:
    #             zult = [(famtab[p.track], p.track) for p in prods]
    #         zult.sort(reverse=True)
    #         return zult

    def enumerate_track_names(self, only_active=False) -> typ.Iterable[typ.Tuple[int, str]]:
        """All the names of the tracks, from highest zorder to lowest

        z>=0 implies an active track in the document,
        <0 implies potentials that have products either cached or potential

        """
        for z, track in self.doc.track_order.items():
            if only_active and z < 0:
                break
            yield z, track

    # def product_state(self, *product_uuids: T.Iterable[UUID]) -> T.Iterable[Flags]:
    #     """Merge document and workspace state information on a given sequence of products
    #     """
    #     uuids = list(product_uuids)
    #     warnings.warn("old-model query of product states, this is a crutch")
    #     # FIXME: implement using new model
    #     cls = self.doc.current_layer_set
    #     ready_uuids = {x.uuid for x in cls}
    #     if not uuids:
    #         uuids = list(ready_uuids)
    #     for uuid in uuids:
    #         s = set()
    #         if uuid in ready_uuids:
    #             s.add(State.READY)
    #         # merge state from workspace
    #         s.update(self.ws.product_state(uuid))
    #         yield s

    def product_state(self, uuid: UUID) -> Flags:
        """Merge document state with workspace state"""
        s = Flags()
        s.update(self.ws.product_state(uuid))
        # s.update(self.doc.product_state.get(prod.uuid) or Flags())
        return s

    def frame_info_for_product(
        self, prod: Product = None, uuid: UUID = None, when_overlaps: Span = None
    ) -> typ.Optional[FrameInfo]:
        """Generate info struct needed for timeline representation, optionally returning None if outside timespan of interest"""
        if prod is None:
            with self.mdb as S:  # this is a potential performance toilet, but OK to use sparsely
                prod = S.query(Product).filter_by(uuid_str=str(uuid)).first()
                return self.frame_info_for_product(prod=prod, when_overlaps=when_overlaps)
        prod_e = prod.obs_time + prod.obs_duration
        if (when_overlaps is not None) and ((prod_e <= when_overlaps.s) or (prod.obs_time >= when_overlaps.e)):
            # does not intersect our desired Span, skip it
            return None
        nfo = prod.info
        # DISPLAY_NAME has DISPLAY_TIME as part of it, FIXME: stop that
        dt = nfo[Info.DISPLAY_TIME]
        dn = nfo[Info.DISPLAY_NAME].replace(dt, "").strip()
        fin = FrameInfo(
            uuid=prod.uuid,
            ident=prod.ident,
            when=Span(prod.obs_time, prod.obs_duration),
            # FIXME: new model old model
            state=self.product_state(prod.uuid),
            primary=dn,
            secondary=dt,  # prod.obs_time.strftime("%Y-%m-%d %H:%M:%S")
            # thumb=
        )
        return fin

    def enumerate_tracks_frames(self, only_active: bool = False, when: Span = None) -> typ.Iterable[TrackInfo]:
        """enumerate tracks as TrackInfo and FrameInfo structures for timeline use, in top-Z to bottom-Z order"""
        if when is None:  # default to the document's Span, either explicit (user-specified) or implicit
            when = self.timeline_span
        with self.mdb as s:
            for z, track in self.doc.track_order.items():  # enumerates from high Z to low Z
                if only_active and (z < 0):
                    break
                fam, ctg = track.split(FCS_SEP)
                LOG.debug("yielding TrackInfo and FrameInfos for {}".format(track))
                frames = []
                # fam_nfo = self.doc.family_info(fam)
                que = s.query(Product).filter((Product.family == fam) & (Product.category == ctg))
                for prod in que.all():
                    frm = self.frame_info_for_product(prod, when_overlaps=when)
                    if frm is not None:
                        frames.append(frm)
                if not frames:
                    LOG.warning("track {} with no frames - skipping (missing files or resources?)".format(track))
                    continue
                LOG.debug("found {} frames for track {}".format(len(frames), track))
                frames.sort(key=lambda x: x.when.s)
                track_span = Span.from_s_e(frames[0].when.s, frames[-1].when.e) if frames else None
                trk = TrackInfo(
                    track=track,
                    presentation=self.doc.family_presentation.get(track),
                    when=track_span,
                    frames=frames,
                    state=Flags(),  # FIXME
                    primary=" ".join(reversed(fam.split(FCS_SEP))),  # fam_nfo[Info.DISPLAY_FAMILY],
                    secondary=" ".join(reversed(ctg.split(FCS_SEP))),
                )
                yield z, trk

    def lock_track_to_frame(self, track: str, frame: UUID = None):
        """User"""
        if frame is None:
            if frame in self.doc.track_frame_locks:
                del self.doc.track_frame_locks[frame]
        else:
            self.doc.track_frame_locks[track] = frame

        # FIXME: signal, since this will cause effects on animation and potentially static display order
        # also may want to set some state Flags on the track?
        # this needs to invalidate the current display and any animation
        self.doc.didChangeLayerVisibility.emit({frame: True})

    def activate_frames(self, frame: UUID, *more_frames: typ.Iterable[UUID]) -> typ.Sequence[UUID]:
        """Activate one or more frames in the document, as directed by the timeline widgets
        :returns sequence of UUIDs actually activated
        """
        queue: TaskQueue = self.doc.queue
        frames = [frame] + list(*more_frames)
        if len(frames) > 1:
            frames = list(reversed(self.doc.sort_product_uuids(frames)))

        def _bgnd_ensure_content_loaded(ws=self.ws, frames=frames):
            ntot = len(frames)
            for nth, frame in enumerate(frames):
                yield {
                    TASK_DOING: "importing {}/{}".format(nth + 1, ntot),
                    TASK_PROGRESS: float(nth + 1) / float(ntot + 1),
                }
                ws.import_product_content(frame)

        def _then_show_frames_in_document(doc=self.doc, frames=frames):
            """finally-do-this section back on UI thread"""
            [self.doc.activate_product_uuid_as_new_dataset(frame) for frame in frames]
            return
            # ensure that the track these frames belongs to is activated itself
            # update the timeline view states of these frames to show them as active as well
            # generate their presentations if needed
            # regenerate the animation plan and refresh the screen

        queue.add(
            "activate frames " + repr(frames),
            _bgnd_ensure_content_loaded(),
            "activate {} frames".format(len(frames)),
            interactive=False,
            and_then=_then_show_frames_in_document,
        )

    def _products_in_track(self, track: str, during: Span = None) -> typ.List[UUID]:
        fam, ctg = track.split(FCS_SEP)

        def uu(x):
            LOG.debug("found product {}".format(x))
            return UUID(x)

        if during is None:
            with self.mdb as S:
                return [
                    uu(x)
                    for x in S.query(Product.uuid_str).filter((Product.family == fam) & (Product.category == ctg)).all()
                ]
        else:
            start, end = during.s, during.e
            with self.mdb as S:
                return [
                    uu(x)
                    for x in S.query(Product.uuid_str)
                    .filter(
                        (Product.family == fam)
                        & (Product.category == ctg)
                        & ~((Product.obs_time > end) | ((Product.obs_time + Product.obs_duration) < start))
                    )
                    .all()
                ]

    def _products_in_tracks(self, tracks: typ.Iterable[str], during: Span = None) -> typ.Iterable[UUID]:
        for track in tracks:
            yield from self._products_in_track(track, during)

    def deactivate_frames(self, frame: UUID, *more_frames: typ.Iterable[UUID]) -> typ.Sequence[UUID]:
        """Activate one or more frames in the document, as directed by the timeline widgets"""
        away_with_thee = list(self.doc.filter_active_layers([frame] + list(more_frames)))
        LOG.debug("about to remove {} layers".format(len(away_with_thee)))
        self.doc.remove_layers_from_all_sets(away_with_thee)
        return away_with_thee

    def deactivate_track(self, track: str):
        LOG.debug("deactivate_track {}".format(track))
        # time_range: Span = self.timeline_span
        pit = self._products_in_track(track)
        if pit:
            self.deactivate_frames(*pit)
        # refresh state of track itself

        # refresh timeline display and layer list
        # defer any signals if there's a context in progress
        self._finally(self.doc.didReorderTracks.emit, set(), {track})

    def activate_track(self, track: str):
        """Activate a track, nudging all frames in the active time range into active state"""
        # time_range: Span = self.timeline_span
        pit = self._products_in_track(track)
        if pit:
            self.activate_frames(*pit)
        # refresh state of track itself

        # send a refresh to the timeline display and layer list
        self._finally(self.doc.didReorderTracks.emit, {track}, set())
        LOG.debug("activate_track {}".format(track))

    # def activate_track_time_range(self, track: str, when: Span, activate: bool=True):
    #     pass
    #
    # def disable_track(self, track:str):
    #     pass

    def move_track(self, track: str, to_z: int):
        was_inactive = self.doc.track_order.move(to_z, track) < 0
        is_inactive = to_z < 0
        if was_inactive ^ is_inactive:  # then state changed
            if is_inactive:
                LOG.debug("deactivating track {}".format(track))
                self.deactivate_track(track)
            else:
                LOG.debug("activating track {}".format(track))
                self.activate_track(track)

    # def reorder_tracks(self, new_order: T.Iterable[T.Tuple[int, str]]):

    def tracks_in_family(self, family: str, only_active: bool = True) -> typ.Sequence[str]:
        """yield track names in document that share a common family"""
        for z, track in self.doc.track_order.items():
            if only_active and z < 0:
                break
            tfam = track.split(FCS_SEP)[0]
            if tfam == family:
                yield track

    def sync_available_tracks(self):
        old_tracks = set(self.doc.track_order.values())
        self.doc.sync_potential_tracks_from_metadata()
        new_tracks = set(self.doc.track_order.values())
        LOG.debug("added these tracks: {}".format(repr(list(sorted(new_tracks - old_tracks)))))
        # FIXME: make sure that tracks with active products are moved up to the active z-levels (>=0)
        # that will require making sure that the z-order matches the layer list order and vice versa
        # delay that transition until we're fully over to a track-centric model, since layer-centric
        # model can have products from multiple tracks stacked in random z order

    # @property
    # def _deferring(self):
    #     "Am I a deferred-action context or an immediate context helper"
    #     return self._actions is not None

    def __init__(self, *args, as_readwrite_context=False, **kwargs):
        super(DocumentAsTrackStack, self).__init__(*args, **kwargs)
        if as_readwrite_context:
            self._actions = []

    def __enter__(self):
        # set up a copy with deferred action until we're done
        return DocumentAsTrackStack(doc=self.doc, mdb=self.mdb, ws=self.ws, as_readwrite_context=True)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            LOG.error("write to DocumentAsTrackStack via read-write context caused an exception")
            raise exc_val
        else:
            # commit code
            while self._actions:
                action = self._actions.pop(0)
                action()


###################################################################################################################


class DocumentAsRegionProbes(DocumentAsContextBase):
    """Document is probed over a variety of regions"""

    def __enter__(self):
        raise NotImplementedError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            pass
        else:
            # commit code
            pass
        raise NotImplementedError()


###################################################################################################################


class DocumentAsStyledFamilies(DocumentAsContextBase):
    """Document is composed of products falling into families, each represented onscreen with a style"""

    def __enter__(self):
        raise NotImplementedError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            pass
        else:
            # commit code
            pass
        raise NotImplementedError()


###################################################################################################################


class DocumentAsResourcePools(DocumentAsContextBase):
    """Document allows user to specify files and directory systems to search for Resources, Products, Content"""

    def __enter__(self):
        raise NotImplementedError()

    @property
    def search_paths(self):
        raise NotImplementedError()

    @search_paths.setter
    def search_paths(self, list_of_paths):
        raise NotImplementedError()

    def add_search_path(self, dirname):
        raise NotImplementedError()

    def interactive_open(self, *file_paths):
        raise NotImplementedError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            pass
        else:
            # commit code
            pass
        raise NotImplementedError()


###################################################################################################################


class DocumentAsRecipeCollection(DocumentAsContextBase):
    def __enter__(self):
        raise NotImplementedError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            pass
        else:
            # commit code
            pass
        raise NotImplementedError()


###################################################################################################################


class AnimationStep(typ.NamedTuple):
    """Animation sequence used by SGM is a series of AnimationSteps, obtained from doc.as_animation_sequence
    SGM uses them to signal other subsystems on playback progress
    A change of presentation for one or more families does not invalidate an animation plan
    """

    plan_id: typ.Any  # a unique per generated plan, used for cache validation purposes
    # how many wall microseconds this frame occurs at and lasts
    offset: int
    duration: int  # 0 if undetermined / infinite
    # back-to-front list of products to present during this timestep
    uuids: typ.Tuple[UUID]
    # corresponding family, to reduce SGM need for queries
    families: typ.Tuple[str]
    # primary kind for displaying the data
    kinds: typ.Tuple[Kind]
    # data time Span this step represents
    data_span: Span


class DocumentAsAnimationSequence(DocumentAsContextBase):
    """Document as sequence of product frames that appear and disappear when animated at a multiple of real-time
    Used principally by SGM and playback slider control (timeline has its own interface)
    """

    @property
    def plan_id(self) -> typ.Any:
        """The plan id is just a hashable unique (compare with "is") saying what the current valid plan is.

        To allow SGM/others to cache.

        """

    def animation_plan(
        self, multiple_of_realtime: float = None, start: datetime = None, stop: datetime = None
    ) -> typ.Sequence[AnimationStep]:
        """Yield series of AnimationStep
        May result in a new plan_id being the valid plan
        """
        return []

    # @property
    # def prez_id(self) -> T.Any:
    #     """An immutable hashable unique which changes when any presentation in the document changes
    #     :return:
    #     """

    @property
    def family_presentation(self) -> typ.Mapping[str, Presentation]:
        """Mapping of families to their presentation tuples.

        Guaranteed to include at least the families participating in the animation.

        """
        return dict(self.doc.family_presentation)

    @property
    def playback_per_sec(self) -> timedelta:
        """The number of data seconds per wall second of playback"""
        return self.doc.playback_per_sec

    @property
    def playhead_step(self) -> AnimationStep:
        """AnimationStep under the current playhead"""

    @property
    def playhead_time(self) -> typ.Optional[datetime]:
        """playhead time, or None if animating"""
        raise NotImplementedError()

    @playhead_time.setter
    def playhead_time(self, when: datetime):
        raise NotImplementedError()

    @property
    def playback_time_range(self) -> Span:
        raise NotImplementedError()

    @playback_time_range.setter
    def playback_time_range(self, start_end: Span):
        # set document playback time range
        # if we're in a with-clause, defer signals until outermost exit
        # if we're not in a with-clause, raise ContextNeededForEditing exception
        raise NotImplementedError()


###################################################################################################################


class ProductDataArrayProxy(object):
    """
    As-pythonic-as-possible dataset proxy for SIFT content.
    Hides document, metadatabase, and workspace accesses into something less scattered.
    Intended to evolve toward being usable within a user-accessible friendly namespace.
    Also intended to provide a stable API behind which we can add incremental compatibility with dask/xarray.

    Metadata keys are accessible by .info dictionary-style, or by query
    Data are accessible by .data, or numpy bracket semantics
    """

    def __init__(self, context, uuid: UUID):
        self._ctx = context
        self._uuid = uuid

    def data(self, field=None):
        """Return an xarray-like entity"""

    @property
    def info(self):
        """Return dictionary-like metadata dictionary"""


class DocumentAsProductArrayCollection(DocumentAsContextBase):
    """Document as MeasurementDatasetProxy object collection
    merges WS, MDB, and Doc
    """

    _proxies = None

    def __init__(self, *args, **kwargs):
        super(DocumentAsProductArrayCollection, self).__init__(*args, **kwargs)
        self._proxies = {}

    def __getitem__(self, product_uuid: UUID) -> ProductDataArrayProxy:

        it = self._proxies.get(product_uuid, None)
        if it is not None:
            return it

        not_in_doc = False
        not_in_ws = False
        try:
            self.doc[product_uuid]
        except KeyError:
            not_in_doc = True
        nfo = self.ws.get_info(product_uuid)
        if nfo is None:
            not_in_ws = True

        if not_in_doc and not_in_ws:
            raise KeyError("UUID {} is unknown".format(product_uuid))

        self._proxies[product_uuid] = it = ProductDataArrayProxy(self, product_uuid)
        return it

    def __enter__(self):
        raise NotImplementedError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            # abort code
            pass
        else:
            # commit code
            pass
        raise NotImplementedError()


###################################################################################################################


class DataLayer:
    """
    A DataLayer is unique to a satellite, channel and calibration
    over a certain period of time.

    Represents images over time belonging to a certain (satellite, channel, calibration)
                            for a given span of time
    """

    def __init__(self, uuids_timestamps: typ.List, product_family_key: typ.Tuple):
        # metadata redundant w.r.t. satellite, channel, calibration, so any metadata object
        #                           from the list of tuples will do
        self._t_matched: typ.Optional[datetime] = None
        self.product_family_key = product_family_key

        # sort timestamps while keeping them synchronous with images
        uuids_tstamps_asc = sorted(uuids_timestamps, key=lambda tup: tup[1])
        # Create timeline according to temporal resolution

        # maps: timestamp -> uuid
        self.timeline = {uuid_dt_tup[1]: uuid_dt_tup[0] for uuid_dt_tup in uuids_tstamps_asc}

    def t_matched_uuid(self) -> typ.Optional[UUID]:
        """
        Helper function for accessing the timeline of a data_layer
        :return: UUID of image with datetime matching t_matched or None if t_matched is not set.
        """
        if self.t_matched is None:
            return None
        return self.timeline[self.t_matched]

    def get_index_of_uuid(self, uuid: UUID) -> int:
        """
        Helper function for getting the index of a UUID within the data_layer's timeline.
        :param uuid: UUID to get index for.
        :return: Index of queried UUID.
        """
        try:
            return list(self.timeline.values()).index(uuid)
        except ValueError:
            raise ValueError(f"UUID not found in timeline of: {str(self)}.")

    @property
    def t_matched(self):
        return self._t_matched

    @t_matched.setter
    def t_matched(self, t_matched):
        if not isinstance(t_matched, datetime):
            raise ValueError("Matched time needs to be datetime object.")
        self._t_matched = t_matched

    @property
    def satellite(self):
        return self.product_family_key[0].value

    @satellite.setter
    def satellite(self, *args, **kwargs):
        raise ValueError("Changing type of a DataLayer forbidden.")

    @property
    def product_str(self):
        return self.product_family_key[2]

    @product_str.setter
    def product_str(self, *args, **kwargs):
        raise ValueError("Changing type of a DataLayer forbidden.")


class DataLayerCollection(QObject):
    """
    Collection of data layers (representation of time series of images) with one data layer
    as driving layer, designated by its ProductFamilyKey.
    """

    didUpdateCollection = pyqtSignal()

    def __init__(self, data_layers: typ.List[DataLayer]):
        super().__init__()
        # dict mapping product family keys to data layers
        self.data_layers = {}
        # Convenience functions must return a data layer. This is used by caller code to i.e. set a
        # new driving layer either according to some policy or an index
        self.convenience_functions: typ.Dict[str, typ.Callable] = {
            "Most Frequent": self.get_most_frequent_data_layer_index
        }
        self.product_family_keys = set()
        if data_layers:
            self.notify_update_collection(data_layers)

    def get_most_frequent_data_layer_index(self) -> int:
        """
        Get index of the data layer with the least mean difference between timestamps.
        :return: -1 if no data layers exist, index of most frequent data layer otherwise.
        """
        num_data_layers = len(self.data_layers.values())
        if num_data_layers == 0:
            return -1
        else:
            temporal_differences = np.zeros(num_data_layers)
            for i, data_layer in enumerate(self.data_layers.values()):
                tl = list(data_layer.timeline.keys())
                t_diffs = [tl[i + 1] - tl[i] for i in range(len(tl) - 1)]
                # Calculate mean difference in timeline in seconds to support
                # comparing timelines with non-regularly occurring timestamps
                temporal_differences[i] = np.mean(list(map(lambda td: td.total_seconds(), t_diffs)))
            most_frequent_data_layer_idx = np.argmin(temporal_differences)
            if not isinstance(most_frequent_data_layer_idx, np.int64):
                # If there exist multiple timelines at the same sampling rate, take the first one.
                most_frequent_data_layer_idx = most_frequent_data_layer_idx[0]
            return most_frequent_data_layer_idx

    def get_data_layer_by_index(self, idx) -> typ.Optional[DataLayer]:
        curr_data_layers = list(self.data_layers.values())
        try:
            return curr_data_layers[idx]
        except IndexError:
            return None

    @property
    def num_data_layers(self):
        return len(list(self.data_layers.values()))

    def notify_update_collection(self, new_data_layers: typ.List[DataLayer]):
        """
        Update state of collection when new data is being loaded or old data is discarded and
        notify dependants via Qt signal.
        Two cases need to be taken into account:
            1) entirely new data layer as shown by previously unknown product family key

            2)data layer with same pfkey already exists
                   -> add new timesteps without duplication
        :return:
        """
        for data_layer in new_data_layers:

            if data_layer.product_family_key in self.product_family_keys:
                old_data_layer = self.data_layers[data_layer.product_family_key]
                # Timeline maps: timestamp -> uuid
                for tstamp, uuid in data_layer.timeline.items():
                    if uuid not in old_data_layer.timeline.values():
                        old_data_layer.timeline[tstamp] = uuid
                self.data_layers[data_layer.product_family_key].timeline = self._sort_timeline(old_data_layer.timeline)
            else:
                self.product_family_keys.add(data_layer.product_family_key)
                self.data_layers[data_layer.product_family_key] = data_layer
        self.didUpdateCollection.emit()

    @staticmethod
    def _sort_timeline(timeline):
        """
        Convenience method to sort timelines of data layers by ordered
        ascending by their timestamps.
        :param timeline: Dict mapping datetime objects to uuids
        :return: Sorted timeline dict
        """
        sorted_tl = sorted(timeline.items(), key=lambda kv: kv[0])
        return {k: v for k, v in sorted_tl}

    def remove_by_uuid(self, uuid_to_remove: UUID):
        for _, data_layer in self.data_layers.items():
            new_timeline = data_layer.timeline.copy()
            for dt, uuid in data_layer.timeline.items():
                if uuid_to_remove == uuid:
                    del new_timeline[dt]
                    # NOTE(mk): Able to exit prematurely here if guaranteed that
                    # no uuid present in multiple data layers
            data_layer.timeline = self._sort_timeline(new_timeline)
        self.didUpdateCollection.emit()


class Document(QObject):  # base class is rightmost, mixins left of that
    """Storage for layer and user information.

    Document is a set of tracks in a Z order, with Z>=0 for "active" tracks the user is working with
    Tracks with Z-order <0 are inactive, but may be displayed in the timeline as potentials for the
    user to drag to active
    Document has a playhead, a playback time range, an active timeline display range
    Tracks and frames (aka Products) can have state information set

    This is the low-level "internal" interface that acts as a signaling hub.
    Direct access to the document is being deprecated.
    Most direct access patterns should be migrated to using a contextual view of the document,
    in order to reduce abstraction leakage and permit the document storage to evolve.
    """

    config_dir: str = None
    queue: TaskQueue = None
    _workspace: BaseWorkspace = None

    # timeline the user has specified:
    track_order: ZList = None  # (zorder, family-name) with higher z above lower z; z<0 should not occur

    # overall visible range of the active data if specified by user,
    # else None means assume use the product timespan from metadatabase
    timeline_span: Span = None

    # playback information
    playhead_time: datetime = None  # document stored playhead time
    playback_per_sec: timedelta = timedelta(seconds=60.0)  # data time increment per wall-second

    # playback time range, if not None is a subset of overall timeline
    playback_span: Span = None

    # user-directed overrides on tracks and frames (products)
    track_state: typ.Mapping[str, Flags] = None
    product_state: typ.Mapping[UUID, Flags] = None

    # user can lock tracks to a single frame throughout
    track_frame_locks: typ.Mapping[str, UUID] = None

    # Maps of family names to their document recipes
    family_presentation: typ.Mapping[str, Presentation] = None
    family_calculation: typ.Mapping[str, object] = None  # algebraic combinations of multiple products

    # DEPRECATION in progress: layer sets
    """
    Document has one or more LayerSets choosable by the user (one at a time) as currentLayerSet
    LayerSets configure animation order, visibility, enhancements and linear combinations
    LayerSets can be cloned from the prior active LayerSet when unconfigured
    Document has Probes, which operate on the currentLayerSet
    Probes have spatial areas (point probes, shaped areas)
    Probe areas are translated into localized data masks against the workspace raw data content
    """
    current_set_index = 0
    _layer_sets = None  # list(DocLayerSet(Presentation, ...) or None)
    _layer_with_uuid = None  # dict(uuid:Doc____Layer)

    # signals
    # Clarification: Layer interfaces migrate to layer meaning "current active products under the playhead"
    # new order list with None for new layer; info-dictionary, overview-content-ndarray
    didAddDataset = pyqtSignal(frozendict, Presentation)
    didAddBasicDataset = pyqtSignal(tuple, UUID, Presentation)  # REMOVE: not emitted anywhere anymore
    didUpdateBasicDataset = pyqtSignal(UUID, Kind)
    # comp layer is derived from multiple basic layers and has its own UUID
    didAddCompositeDataset = pyqtSignal(tuple, UUID, Presentation)
    didAddLinesDataset = pyqtSignal(tuple, UUID, Presentation)  # REMOVE: not emitted anywhere anymore
    didAddPointsDataset = pyqtSignal(tuple, UUID, Presentation)  # REMOVE: not emitted anywhere anymore
    # new order, UUIDs that were removed from current layer set, first row removed, num rows removed
    didRemoveDatasets = pyqtSignal(tuple, list, int, int)
    willPurgeDataset = pyqtSignal(UUID)  # UUID of the layer being removed
    didReorderDatasets = pyqtSignal(tuple)  # list of original indices in their new order, None for new layers
    didChangeLayerVisibility = pyqtSignal(dict)  # {UUID: new-visibility, ...} for changed layers
    didReorderAnimation = pyqtSignal(tuple)  # list of UUIDs representing new animation order
    didChangeLayerName = pyqtSignal(UUID, str)  # layer uuid, new name
    # new layerset number typically 0..3
    # list of Presentation tuples representing new display order, new animation order
    didSwitchLayerSet = pyqtSignal(int, DocLayerStack, tuple)
    didChangeColormap = pyqtSignal(dict)  # dict of {uuid: colormap-name-or-UUID, ...} for all changed layers
    didChangeColorLimits = pyqtSignal(dict)  # dict of {uuid: (vmin, vmax), ...} for all changed layers
    didChangeGamma = pyqtSignal(dict)  # dict of {uuid: gamma float, ...} for all changed layers
    didChangeComposition = pyqtSignal(tuple, UUID, Presentation)  # new-layer-order, changed-layer, new-Presentation
    didChangeCompositions = pyqtSignal(tuple, tuple, tuple)  # new-layer-order, changed-layers, new-prezs
    didCalculateLayerEqualizerValues = pyqtSignal(
        dict
    )  # dict of {uuid: (value, normalized_value_within_clim)} for equalizer display
    didChangeProjection = pyqtSignal(str)  # name of projection (area definition)
    # didChangeShapeLayer = pyqtSignal(dict)
    didAddFamily = pyqtSignal(str, dict)  # name of the newly added family and dict of family info
    didRemoveFamily = pyqtSignal(str)  # name of the newly added family and dict of family info
    didReorderTracks = pyqtSignal(set, set)  # added track names, removed track names
    didChangeImageKind = pyqtSignal(dict)

    # high-level contexts providing purposed access to low-level document and its storage, as well as MDB and WS
    # layer display shows active products under the playhead
    as_layer_stack: DocumentAsLayerStack = None
    # track display shows all available products according to metadatabase;
    # some tracks are active, i.e. they have are allowed to present as part of document
    as_track_stack: DocumentAsTrackStack = None
    # presentation migrates from being layer-owned to being family-owned
    as_styled_families: DocumentAsStyledFamilies = None
    # metadata is collected from resource pools, which can be local directories or remote URIs
    as_resource_pools: DocumentAsResourcePools = None
    # presentation, calculation, and composition recipes are part of the document
    as_recipe_collection: DocumentAsRecipeCollection = None
    # SceneGraphManager needs a plan on how to present and animate product content
    as_animation_sequence: DocumentAsAnimationSequence = None
    # content gets probes applied across points and regions
    as_region_probes: DocumentAsRegionProbes = None

    def __init__(
        self,
        workspace: BaseWorkspace,
        queue,
        config_dir=DOCUMENT_SETTINGS_DIR,
        layer_set_count=DEFAULT_LAYER_SET_COUNT,
        **kwargs,
    ):
        super(Document, self).__init__(**kwargs)
        self.config_dir = config_dir
        self.queue = queue
        if not os.path.isdir(self.config_dir):
            LOG.debug("Creating settings directory {}".format(self.config_dir))
            os.makedirs(self.config_dir)

        # high level context managers provide access patterns for use-case based behaviors
        self.as_layer_stack = DocumentAsLayerStack(self, workspace.metadatabase, workspace)
        self.as_track_stack = DocumentAsTrackStack(self, workspace.metadatabase, workspace)
        self.as_styled_families = DocumentAsStyledFamilies(self, workspace.metadatabase, workspace)
        self.as_resource_pools = DocumentAsResourcePools(self, workspace.metadatabase, workspace)
        self.as_recipe_collection = DocumentAsRecipeCollection(self, workspace.metadatabase, workspace)
        self.as_animation_sequence = DocumentAsAnimationSequence(self, workspace.metadatabase, workspace)
        self.as_region_probes = DocumentAsRegionProbes(self, workspace.metadatabase, workspace)

        self._workspace = workspace
        self._layer_sets = [DocLayerStack(self)] + [None] * (layer_set_count - 1)
        self._layer_with_uuid = {}

        # data_layers represents time series of images ordered by product family
        self.data_layers: typ.List[DataLayer] = []
        self.data_layer_collection: DataLayerCollection = DataLayerCollection([])

        self.colormaps = COLORMAP_MANAGER
        self.default_area_def_name = AreaDefinitionsManager.default_area_def_name()
        self.current_area_def_name = self.default_area_def_name
        # HACK: This should probably be part of the metadata database in the future
        self._families = defaultdict(list)

        # Create directory if it does not exist
        cmap_base_dir = os.path.join(self.config_dir, "colormaps")
        read_cmap_dir = os.path.join(cmap_base_dir, "site")  # read-only
        write_cmap_dir = os.path.join(cmap_base_dir, "user")  # writeable
        self.read_cmap_dir = read_cmap_dir
        self.write_cmap_dir = write_cmap_dir
        importable_cmap_cats = [(True, SITE_CATEGORY, read_cmap_dir), (False, USER_CATEGORY, write_cmap_dir)]
        for read_only, cmap_cat, cmap_dir in importable_cmap_cats:
            if not os.path.exists(cmap_dir):
                os.makedirs(cmap_dir)
            else:
                self.colormaps.import_colormaps(cmap_dir, read_only=read_only, category=cmap_cat)

        # timeline document storage setup with initial track order and time range
        self.product_state = defaultdict(Flags)
        self.track_state = defaultdict(Flags)
        self.track_order = ZList()
        self.track_frame_locks = {}
        self.family_calculation = {}
        self.family_composition = {}
        self.family_presentation = {}

        # scan available metadata for initial state
        # FIXME: refresh this once background scan finishes and new products are found
        # self.timeline_span = self.playback_span = self.potential_product_span()
        if isinstance(self._workspace, CachingWorkspace):
            self.sync_potential_tracks_from_metadata()

    def potential_product_span(self) -> typ.Optional[Span]:
        with self._workspace.metadatabase as S:
            all_times = list(S.query(Product.obs_time, Product.obs_duration).distinct())
        if not all_times:
            return None
        starts = [s for (s, d) in all_times]
        ends = [(s + d) for (s, d) in all_times]
        s = min(starts)
        e = max(ends)
        return Span(s, e - s)

    def potential_tracks(self) -> typ.List[str]:
        """List the names of available tracks (both active and potential) according to the metadatabase"""
        with self._workspace.metadatabase as S:
            return list((f + FCS_SEP + c) for (f, c) in S.query(Product.family, Product.category).distinct())

    def sync_potential_tracks_from_metadata(self):
        """update track_order to include any newly available tracks"""
        all_tracks = list(self.potential_tracks())
        all_tracks.sort()
        old_tracks = set(name for z, name in self.track_order.items())
        for track in all_tracks:
            self.track_order.append(track, start_negative=True, not_if_present=True)
        for dismissed in old_tracks - set(all_tracks):
            LOG.debug("removing track {} from track_order".format(dismissed))
            self.track_order.remove(dismissed)
        new_tracks = set(name for z, name in self.track_order.items())
        if old_tracks != new_tracks:
            LOG.info("went from {} available tracks to {}".format(len(old_tracks), len(new_tracks)))
            self.didReorderTracks.emit(new_tracks - old_tracks, old_tracks - new_tracks)

    def find_colormap(self, colormap):
        if isinstance(colormap, str) and colormap in self.colormaps:
            colormap = self.colormaps[colormap]
        return colormap

    def area_definition(self, area_definition_name=None):
        return AreaDefinitionsManager.area_def_by_name(area_definition_name or self.current_area_def_name)

    def change_projection(self, area_def_name=None):
        if area_def_name is None:
            area_def_name = self.default_area_def_name
        assert area_def_name in AreaDefinitionsManager.available_area_def_names()
        if area_def_name != self.current_area_def_name:
            LOG.debug(
                f"Changing projection (area definition) from" f" '{self.current_area_def_name}' to '{area_def_name}'"
            )
            self.current_area_def_name = area_def_name
            self.didChangeProjection.emit(self.current_area_def_name)

    def update_user_colormap(self, colormap, name):
        # Update new gradient into save location
        try:
            filepath = self.write_cmap_dir
            cmap_file = open(os.path.join(filepath, name + ".json"), "w")
            cmap_file.write(json.dumps(colormap, indent=2, sort_keys=True))
            cmap_file.close()
        except IOError:
            LOG.error("Error saving gradient: {}".format(name), exc_info=True)

        cmap = PyQtGraphColormap(colormap)
        self.colormaps[name] = cmap

        # Update live map
        uuids = [p.uuid for _, p, _ in self.current_layers_where(colormaps=[name])]
        self.change_colormap_for_layers(name, uuids)

    def remove_user_colormap(self, name):
        try:
            os.remove(os.path.join(self.config_dir, "colormaps", "user", name + ".json"))
        except OSError:
            pass

        del self.colormaps[name]

    def current_projection_index(self):
        return list(AreaDefinitionsManager.available_area_def_names()).index(self.current_area_def_name)

    def change_projection_index(self, idx):
        return self.change_projection(tuple(AreaDefinitionsManager.available_area_def_names())[idx])

    @property
    def current_layer_set(self):
        cls = self._layer_sets[self.current_set_index]
        assert isinstance(cls, DocLayerStack)
        return cls

    def _insert_layer_with_info(self, info: DocDataset, cmap=None, style=None, insert_before=0):
        """
        insert a layer into the presentations but do not signal
        :return: new Presentation tuple, new reordered indices tuple
        """
        if cmap is None:
            cmap = info.get(Info.COLORMAP)
        if style is None:
            style = info.get(Info.STYLE)
        gamma = 1.0
        if isinstance(info, DocRGBDataset):
            gamma = (1.0,) * 3
        elif hasattr(info, "layers"):
            gamma = (1.0,) * len(info.layers)

        # get the presentation for another layer in our family
        family_uuids = self.family_uuids(info[Info.FAMILY])
        family_prez = self.prez_for_uuid(family_uuids[0]) if family_uuids else None
        p = Presentation(
            uuid=info[Info.UUID],
            kind=info[Info.KIND],
            visible=True,
            a_order=None,
            colormap=cmap if family_prez is None else family_prez.colormap,
            style=style if family_prez is None else family_prez.style,
            climits=info[Info.CLIM] if family_prez is None else family_prez.climits,
            gamma=gamma if family_prez is None else family_prez.gamma,
            mixing=Mixing.NORMAL,
            opacity=1.0,
        )

        q = dataclasses.replace(p, visible=False)  # make it available but not visible in other layer sets
        old_layer_count = len(self._layer_sets[self.current_set_index])
        for dex, lset in enumerate(self._layer_sets):
            if lset is not None:  # uninitialized layer sets will be None
                lset.insert(insert_before, p if dex == self.current_set_index else q)

        reordered_indices = tuple(
            [None] + list(range(old_layer_count))
        )  # FIXME: this should obey insert_before, currently assumes always insert at top
        return p, reordered_indices

    def activate_product_uuid_as_new_dataset(self, uuid: UUID, insert_before=0, **importer_kwargs):
        if uuid in self._layer_with_uuid:
            LOG.debug("Layer already loaded: {}".format(uuid))
            active_content_data = self._workspace.import_product_content(uuid, **importer_kwargs)
            return uuid, self[uuid], active_content_data

        # FUTURE: Load this async, the slots for the below signal need to be OK
        # with that
        active_content_data = self._workspace.import_product_content(uuid, **importer_kwargs)
        # updated metadata with content information (most importantly navigation
        # information)
        info = self._workspace.get_info(uuid)
        assert info is not None
        LOG.debug("cell_width: {}".format(repr(info[Info.CELL_WIDTH])))

        LOG.debug("new layer info: {}".format(repr(info)))
        self._layer_with_uuid[uuid] = dataset = DocBasicDataset(self, info)
        if Info.UNIT_CONVERSION not in info:
            dataset[Info.UNIT_CONVERSION] = units_conversion(dataset)
        if Info.FAMILY not in dataset:
            dataset[Info.FAMILY] = self.family_for_product_or_layer(dataset)
        presentation, reordered_indices = self._insert_layer_with_info(dataset, insert_before=insert_before)

        # signal updates from the document
        self.didAddDataset.emit(info, presentation)

        return uuid, dataset, active_content_data

    def family_for_product_or_layer(self, uuid_or_layer):
        if isinstance(uuid_or_layer, UUID):
            if isinstance(self._workspace, CachingWorkspace):
                with self._workspace.metadatabase as s:
                    fam = s.query(Product.family).filter_by(uuid_str=str(uuid_or_layer)).first()
            if isinstance(self._workspace, SimpleWorkspace):
                fam = self._workspace.get_info(uuid_or_layer)[Info.FAMILY]
            if fam:
                return fam[0]
            uuid_or_layer = self[uuid_or_layer]
        if Info.FAMILY in uuid_or_layer:
            LOG.debug("using pre-existing family {}".format(uuid_or_layer[Info.FAMILY]))
            return uuid_or_layer[Info.FAMILY]
        # kind:pointofreference:measurement:wavelength
        kind = uuid_or_layer[Info.KIND]
        refpoint = "unknown"  # FUTURE: geo/leo
        measurement = uuid_or_layer.get(Info.STANDARD_NAME)
        if uuid_or_layer.get("recipe"):
            # RGB
            subcat = uuid_or_layer["recipe"].name
        elif uuid_or_layer.get(Info.CENTRAL_WAVELENGTH):
            # basic band
            subcat = uuid_or_layer[Info.CENTRAL_WAVELENGTH]
        else:
            # higher level product or algebraic layer
            subcat = uuid_or_layer[Info.DATASET_NAME]
        return "{}:{}:{}:{}".format(kind.name, refpoint, measurement, subcat)

    def _add_layer_family(self, layer):
        family = layer[Info.FAMILY]
        is_new = family not in self._families or not len(self._families[family])
        self._families[family].append(layer[Info.UUID])
        if is_new:
            self.didAddFamily.emit(family, self.family_info(family))
        return family

    def _remove_layer_from_family(self, uuid):
        family = self[uuid][Info.FAMILY]
        self._families[family].remove(uuid)

        if not self._families[family]:
            # remove the family entirely if it is empty
            LOG.debug("Removing empty family: {}".format(family))
            del self._families[family]
            self.didRemoveFamily.emit(family)

    def family_info(self, family_or_layer_or_uuid):
        family = layer = family_or_layer_or_uuid
        if isinstance(family_or_layer_or_uuid, UUID):
            layer = self[family_or_layer_or_uuid]
        if isinstance(layer, DocBasicDataset):
            family = layer[Info.FAMILY]

        # one layer that represents all the layers in this family
        family_rep = self[self._families[family][0]]

        # convert family subcategory to displayable name
        # if isinstance(family[2], UUID):
        #     # RGB Recipes, this needs more thinking
        #     display_family = family_rep[Info.SHORT_NAME]
        # elif not isinstance(family[2], str):
        #     display_family = "{:.02f} µm".format(family[2])
        # else:
        #     display_family = family[2]
        family_name_components = family.split(":")
        display_family = family_rep[Info.SHORT_NAME] + " " + " ".join(reversed(family_name_components))
        # display_family = str(family)

        # NOTE: For RGBs the SHORT_NAME will update as the RGB changes
        return {
            Info.VALID_RANGE: family_rep[Info.VALID_RANGE],
            Info.UNIT_CONVERSION: family_rep[Info.UNIT_CONVERSION],
            Info.SHORT_NAME: family_rep[Info.SHORT_NAME],
            Info.UNITS: family_rep[Info.UNITS],
            Info.KIND: family_rep[Info.KIND],
            Info.DISPLAY_FAMILY: display_family,
        }

    def import_files(self, paths, insert_before=0, **importer_kwargs) -> dict:
        """Load product metadata and content from provided file paths.

        :param paths: paths to open
        :param insert_before: where to insert them in layer list
        :return:

        """

        # NOTE: if the importer argument 'merge_with_existing' is not set it
        # defaults to True here.
        # TODO(AR) make 'merge_with_existing' an explicit argument to this
        #  method.
        do_merge_with_existing = importer_kwargs.get("merge_with_existing", True) and not importer_kwargs.get(
            "resampling_info"
        )
        # Load all the metadata so we can sort the files
        # assume metadata collection is in the most user-friendly order
        infos = self._workspace.collect_product_metadata_for_paths(paths, **importer_kwargs)
        uuids = []
        merge_target_uuids = {}  # map new files uuids to merge target uuids
        total_products = 0
        for dex, (num_prods, info) in enumerate(infos):
            assert info is not None

            uuid = info[Info.UUID]
            merge_target_uuid = uuid
            if do_merge_with_existing:
                # real_paths because for satpy imports the methods paths parameter actually
                # contains the reader names
                real_paths = info["paths"]
                merge_target = self._workspace.find_merge_target(uuid, real_paths, info)
                if merge_target:
                    merge_target_uuid = merge_target.uuid

            yield {
                TASK_DOING: "Collecting metadata {}/{}".format(dex + 1, num_prods),
                TASK_PROGRESS: float(dex + 1) / float(num_prods),
                "uuid": merge_target_uuid,
                "num_products": num_prods,
            }
            # redundant but also more explicit than depending on num_prods
            total_products = num_prods
            uuids.append(uuid)
            merge_target_uuids[uuid] = merge_target_uuid

        if not total_products:
            raise ValueError("no products available in {}".format(paths))

        if isinstance(self._workspace, CachingWorkspace):
            # reverse list since we always insert a top layer
            uuids = list(reversed(self.sort_product_uuids(uuids)))

        # collect product and resource information but don't yet import content
        for dex, uuid in enumerate(uuids):
            merge_target_uuid = merge_target_uuids[uuid]
            if do_merge_with_existing and uuid != merge_target_uuid:  # merge products
                active_content_data = self._workspace.import_product_content(
                    uuid, merge_target_uuid=merge_target_uuid, **importer_kwargs
                )
                # active_content_data is none if all segments are already loaded
                # and there is nothing new to import
                if active_content_data:
                    dataset = self[merge_target_uuid]
                    self.didUpdateBasicDataset.emit(merge_target_uuid, dataset[Info.KIND])
            elif uuid in self._layer_with_uuid:
                LOG.warning("layer with UUID {} already in document?".format(uuid))
                self._workspace.get_content(uuid)
            else:
                self.activate_product_uuid_as_new_dataset(uuid, insert_before=insert_before, **importer_kwargs)

            yield {
                TASK_DOING: "Loading content {}/{}".format(dex + 1, total_products),
                TASK_PROGRESS: float(dex + 1) / float(total_products),
                "uuid": merge_target_uuid,
                "num_products": total_products,
            }

    def sort_paths(self, paths):
        """
        :param paths: list of paths
        :return: list of paths
        """
        warnings.warn(
            "sort_paths is deprecated in favor of sort_product_uuids, "
            "since more than one product may reside in a resource",
            DeprecationWarning,
        )
        return list(sorted(paths, key=lambda p: os.path.basename(p)))

    def sort_product_uuids(self, uuids: typ.Iterable[UUID]) -> typ.List[UUID]:
        uuidset = set(str(x) for x in uuids)
        if not uuidset:
            return []
        with self._workspace.metadatabase as S:
            zult = [
                (x.uuid, x.ident)
                for x in S.query(Product)
                .filter(Product.uuid_str.in_(uuidset))
                .order_by(Product.family, Product.category, Product.serial)
                .all()
            ]
        LOG.debug("sorted products: {}".format(repr(zult)))
        return [u for u, _ in zult]

    def time_label_for_uuid(self, uuid):
        """used to update animation display when a new frame is shown"""
        if not uuid:
            return "YYYY-MM-DD HH:MM"
        info = self._layer_with_uuid[uuid]
        return info.get(Info.DISPLAY_TIME, "--:--")

    def prez_for_uuids(self, uuids: typ.List[UUID], lset: list = None) -> typ.Iterable[typ.Tuple[UUID, Presentation]]:
        if lset is None:
            lset = self.current_layer_set
        for p in lset:
            if p.uuid in uuids:
                yield p.uuid, p

    def prez_for_uuid(self, uuid: UUID, lset: list = None) -> Presentation:
        for _, p in self.prez_for_uuids((uuid,), lset=lset):
            return p

    def colormap_for_uuids(self, uuids: typ.List[UUID], lset: list = None) -> typ.Iterable[typ.Tuple[UUID, str]]:
        for u, p in self.prez_for_uuids(uuids, lset=lset):
            yield u, p.colormap

    def colormap_for_uuid(self, uuid: UUID, lset: list = None) -> str:
        for _, p in self.colormap_for_uuids((uuid,), lset=lset):
            return p

    def valid_range_for_uuid(self, uuid):
        # Limit ourselves to what information
        # in the future valid range may be different than the default CLIMs
        return self[uuid][Info.CLIM]

    def convert_value(self, uuid, x, inverse=False):
        return self[uuid][Info.UNIT_CONVERSION][1](x, inverse=inverse)

    def format_value(self, uuid, x, numeric=True, units=True):
        return self[uuid][Info.UNIT_CONVERSION][2](x, numeric=numeric, units=units)

    def _get_equalizer_values_image(self, lyr, pinf, xy_pos):
        value = self._workspace.get_content_point(pinf.uuid, xy_pos)
        unit_info = lyr[Info.UNIT_CONVERSION]
        nc, xc = unit_info[1](np.array(pinf.climits))
        # calculate normalized bar width relative to its current clim
        new_value = unit_info[1](value)
        if nc > xc:  # sometimes clim is swapped to reverse color scale
            nc, xc = xc, nc
        if np.isnan(new_value):
            return None
        else:
            if xc == nc:
                bar_width = 0
            else:
                bar_width = (np.clip(new_value, nc, xc) - nc) / (xc - nc)
            return new_value, bar_width, unit_info[2](new_value, numeric=False)

    def _get_equalizer_values_rgb(self, lyr, pinf, xy_pos):
        # We can show a valid RGB
        # Get 3 values for each channel
        # XXX: Better place for this?
        def _sci_to_rgb(v, cmin, cmax):
            if np.isnan(v):
                return None

            if cmin == cmax:
                return 0
            elif cmin > cmax:
                if v > cmin:
                    v = cmin
                elif v < cmax:
                    v = cmax
            else:
                if v < cmin:
                    v = cmin
                elif v > cmax:
                    v = cmax

            return int(round(abs(v - cmin) / abs(cmax - cmin) * 255.0))

        values = []
        for dep_lyr, clims in zip(lyr.layers[:3], pinf.climits):
            if dep_lyr is None:
                values.append(None)
            elif clims is None or clims[0] is None:
                values.append(None)
            else:
                value = self._workspace.get_content_point(dep_lyr[Info.UUID], xy_pos)
                values.append(_sci_to_rgb(value, clims[0], clims[1]))

        nc = 0
        xc = 255
        bar_widths = [(np.clip(value, nc, xc) - nc) / (xc - nc) for value in values if value is not None]
        bar_width = np.mean(bar_widths) if len(bar_widths) > 0 else 0
        values = ",".join(["{:3d}".format(v if v is not None else 0) for v in values])
        return values, bar_width, values

    def update_equalizer_values(self, probe_name, state, xy_pos, uuids=None):
        """user has clicked on a point probe; determine relative and absolute values for all document image layers"""
        # if the point probe was turned off then we don't want to have the equalizer
        if not state:
            self.didCalculateLayerEqualizerValues.emit({})
            return

        if uuids is None:
            uuid_prezs = [(pinf.uuid, pinf) for pinf in self.current_layer_set]
        else:
            uuid_prezs = self.prez_for_uuids(uuids)
        zult = {}
        for uuid, pinf in uuid_prezs:
            try:
                lyr = self._layer_with_uuid[uuid]
                if lyr[Info.KIND] in {Kind.IMAGE, Kind.COMPOSITE, Kind.CONTOUR}:
                    zult[uuid] = self._get_equalizer_values_image(lyr, pinf, xy_pos)
                elif lyr[Info.KIND] == Kind.RGB:
                    zult[uuid] = self._get_equalizer_values_rgb(lyr, pinf, xy_pos)
            except ValueError:
                LOG.warning("Could not get equalizer values for {}".format(uuid))
                zult[uuid] = (0, 0, 0)

        self.didCalculateLayerEqualizerValues.emit(zult)  # is picked up by layer list model to update display

    def _clone_layer_set(self, existing_layer_set):
        return DocLayerStack(existing_layer_set)

    @property
    def current_animation_order(self):
        """
        return tuple of UUIDs representing the animation order in the currently selected layer set
        :return: tuple of UUIDs
        """
        return self.current_layer_set.animation_order

    @property
    def current_layer_uuid_order(self):
        """
        list of UUIDs (top to bottom) currently being displayed, independent of visibility/validity
        :return:
        """
        return tuple(x.uuid for x in self.current_layer_set)

    @property
    def current_visible_layer_uuid(self):
        """
        :return: the topmost visible layer's UUID
        """
        for x in self.current_layer_set:
            layer = self._layer_with_uuid[x.uuid]
            if x.visible and layer.is_valid:
                return x.uuid
        return None

    @property
    def current_visible_layer_uuids(self):
        for x in self.current_layer_set:
            layer = self._layer_with_uuid[x.uuid]
            if x.visible and layer.is_valid:
                yield x.uuid

    # TODO: add a document style guide which says how different bands from different instruments are displayed

    @property
    def active_layer_order(self):
        """
        return list of valid (can-be-displayed) layers which are either visible or in the animation order
        typically this is used by the scenegraphmanager to synchronize the scenegraph elements
        :return: sequence of (layer_prez, layer) pairs, with order=0 for non-animating layers
        """
        for layer_prez in self.current_layer_set:
            if layer_prez.visible or layer_prez.a_order is not None:
                layer = self._layer_with_uuid[layer_prez.uuid]
                if not layer.is_valid:
                    # we don't have enough information to display this layer yet, it's still loading or being configured
                    continue
                yield layer_prez, layer

    def layers_where(self, is_valid=None, is_active=None, in_type_set=None, have_proj=None):
        """
        query current layer set for layers matching criteria
        :param is_valid: None, or True/False whether layer is valid (could be displayed)
        :param is_active: None, or True/False whether layer is active (valid & (visible | animatable))
        :param in_type_set: None, or set of Python types that the layer falls into
        :return: sequence of layers in no particular order
        """
        for layer_prez in self.current_layer_set:
            layer = self._layer_with_uuid[layer_prez.uuid]
            valid = layer.is_valid
            if is_valid is not None:
                if valid != is_valid:
                    continue
            if is_active is not None:
                active = valid and (layer_prez.visible or layer_prez.a_order is not None)
                if active != is_active:
                    continue
            if in_type_set is not None:
                if type(layer) not in in_type_set:
                    continue
            if have_proj is not None:
                if layer[Info.PROJ] != have_proj:
                    continue
            yield layer

    def select_layer_set(self, layer_set_index: int):
        """Change the selected layer set, 0..N (typically 0..3), cloning the old set if needed
        emits docDidChangeLayerOrder with an empty list implying complete reassessment,
        if cloning of layer set didn't occur

        :param layer_set_index: which layer set to switch to

        """

        # the number of layer sets is no longer fixed, but you can't select more than 1 beyond the end of the list!
        assert layer_set_index <= len(self._layer_sets) and layer_set_index >= 0

        # if we are adding a layer set, do that now
        if layer_set_index == len(self._layer_sets):
            self._layer_sets.append(None)

        # if the selected layer set doesn't exist yet, clone another set to make it
        if self._layer_sets[layer_set_index] is None:
            self._layer_sets[layer_set_index] = self._clone_layer_set(self._layer_sets[self.current_set_index])

        # switch to the new layer set and set off events to let others know about the change
        self.current_set_index = layer_set_index
        self.didSwitchLayerSet.emit(layer_set_index, self.current_layer_set, self.current_animation_order)

    def row_for_uuid(self, *uuids):
        d = dict((q.uuid, i) for i, q in enumerate(self.current_layer_set))
        if len(uuids) == 1:
            return d[uuids[0]]
        else:
            return [d[x] for x in uuids]

    def toggle_layer_visibility(self, rows_or_uuids, visible=None):
        """
        change the visibility of a layer or layers
        :param rows_or_uuids: layer index or index list, 0..n-1, alternately UUIDs of layers
        :param visible: True, False, or None (toggle)
        """
        L = self.current_layer_set
        zult = {}
        if isinstance(rows_or_uuids, int) or isinstance(rows_or_uuids, UUID):
            rows_or_uuids = [rows_or_uuids]
        for dex in rows_or_uuids:
            if isinstance(dex, UUID):
                dex = L.index(dex)  # returns row index
            old = L[dex]
            vis = (not old.visible) if visible is None else visible
            nu = dataclasses.replace(old, visible=vis)
            L[dex] = nu
            zult[nu.uuid] = nu.visible
        self.didChangeLayerVisibility.emit(zult)

    def animation_changed_visibility(self, changes):
        """
        this is triggered by animation being stopped,
        via signal scenegraphmanager.didChangeLayerVisibility
        in turn we generate our own didChangeLayerVisibility to ensure document views are up to date
        :param changes: dictionary of {uuid:bool} with new visibility state
        :return:
        """
        curr_set = self.current_layer_set
        for uuid, visible in changes.items():
            dex = curr_set[uuid]
            old = curr_set[dex]
            curr_set[dex] = dataclasses.replace(old, visible=visible)
        self.didChangeLayerVisibility.emit(changes)

    def next_last_step(self, uuid, delta=0, bandwise=False):
        """
        given a selected layer uuid,
        find the next or last time/bandstep (default: the layer itself) in the document
        make all layers in the sibling group invisible save that timestep
        :param uuid: layer we're focusing on as reference
        :param delta: -1 => last step, 0 for focus step, +1 for next step
        :param bandwise: True if we want to change by band instead of time
        :return: UUID of new focus layer
        """
        if bandwise:  # next or last band
            consult_guide = self.channel_siblings
        else:
            consult_guide = self.time_siblings
        sibs, dex = consult_guide(uuid)
        # LOG.debug('layer {0} family is +{1} of {2!r:s}'.format(uuid, dex, sibs))
        if not sibs:
            LOG.info("nothing to do in next_last_timestep")
            self.toggle_layer_visibility(uuid, True)
            return uuid
        dex += delta + len(sibs)
        dex %= len(sibs)
        new_focus = sibs[dex]
        del sibs[dex]
        if sibs:
            LOG.debug("hiding {} siblings".format(len(sibs)))
            self.toggle_layer_visibility(sibs, False)
        LOG.debug("showing new preferred {}step {}".format("time" if not bandwise else "band", new_focus))
        self.toggle_layer_visibility(new_focus, True)  # FUTURE: do these two commands in one step
        return new_focus

    def is_layer_visible(self, row):
        return self.current_layer_set[row].visible

    def layer_animation_order(self, layer_number):
        return self.current_layer_set[layer_number].a_order

    def change_layer_name(self, row, new_name):
        uuid = self.current_layer_set[row].uuid if not isinstance(row, UUID) else row
        info = self._layer_with_uuid[uuid]
        assert uuid == info[Info.UUID]
        if not new_name:
            # empty string, reset to default DISPLAY_NAME
            new_name = info.default_display_name
        info[Info.DISPLAY_NAME] = new_name
        self.didChangeLayerName.emit(uuid, new_name)

    def change_colormap_for_layers(self, name, uuids=None):
        L = self.current_layer_set
        if uuids is not None:
            uuids = self.time_siblings_uuids(uuids)
        else:  # all data layers
            uuids = [pinfo.uuid for pinfo in L]

        nfo = {}
        for uuid in uuids:
            for dex, pinfo in enumerate(L):
                if pinfo.uuid == uuid:
                    L[dex] = dataclasses.replace(pinfo, colormap=name)
                    nfo[uuid] = name
        self.didChangeColormap.emit(nfo)

    def current_layers_where(self, kinds=None, uuids=None, dataset_names=None, wavelengths=None, colormaps=None):
        """check current layer list for criteria and yield"""
        L = self.current_layer_set
        for idx, p in enumerate(L):
            if (uuids is not None) and (p.uuid not in uuids):
                continue
            layer = self._layer_with_uuid[p.uuid]
            if (kinds is not None) and (layer.kind not in kinds):
                continue
            if (dataset_names is not None) and (layer[Info.DATASET_NAME] not in dataset_names):
                continue
            if (wavelengths is not None) and (layer.get(Info.CENTRAL_WAVELENGTH) not in wavelengths):
                continue
            if (colormaps is not None) and (p.colormap not in colormaps):
                continue
            yield (idx, p, layer)

    def change_clims_for_layers_where(self, clims, **query):
        """
        query using .current_layers_where() and change clims en masse
        :param clims: new color limits consistent with layer's presentation
        :param query: see current_layers_where()
        :return:
        """
        nfo = {}
        L = self.current_layer_set
        for idx, pz, layer in self.current_layers_where(**query):
            new_pz = dataclasses.replace(pz, climits=clims)
            nfo[layer.uuid] = new_pz.climits
            L[idx] = new_pz
        self.didChangeColorLimits.emit(nfo)

    def change_clims_for_siblings(self, uuid, clims):
        uuids = self.time_siblings(uuid)[0]
        return self.change_clims_for_layers_where(clims, uuids=uuids)

    def change_gamma_for_layers_where(self, gamma, **query):
        nfo = {}
        L = self.current_layer_set
        for idx, pz, layer in self.current_layers_where(**query):
            new_pz = dataclasses.replace(pz, gamma=gamma)
            nfo[layer.uuid] = new_pz.gamma
            L[idx] = new_pz
        self.didChangeGamma.emit(nfo)

    def change_gamma_for_siblings(self, uuid, gamma):
        uuids = self.time_siblings(uuid)[0]
        return self.change_gamma_for_layers_where(gamma, uuids=uuids)

    def change_layers_image_kind(self, uuids, new_kind):
        """Change an image or contour layer to present as a different kind."""
        nfo = {}
        layer_set = self.current_layer_set
        assert new_kind in Kind
        all_uuids = set()
        for u in uuids:
            fam = self.family_for_product_or_layer(u)
            all_uuids.update(self._families[fam])
        for idx, pz, layer in self.current_layers_where(uuids=all_uuids):
            if pz.kind not in [Kind.IMAGE, Kind.CONTOUR]:
                LOG.warning("Can't change image kind for Kind: %s", pz.kind.name)
                continue
            new_pz = dataclasses.replace(pz, kind=new_kind)
            nfo[layer.uuid] = new_pz
            layer_set[idx] = new_pz
        self.didChangeImageKind.emit(nfo)

    def create_algebraic_composite(self, operations, namespace, info=None, insert_before=0):
        if info is None:
            info = {}

        # Map a UUID's short name to the variable name in the namespace
        # Keep track of multiple ns variables being the same UUID
        short_name_to_ns_name = {}
        for k, u in namespace.items():
            sname = self[u][Info.SHORT_NAME]
            short_name_to_ns_name.setdefault(sname, []).append(k)

        namespace_siblings = {k: self.time_siblings(u)[0] for k, u in namespace.items()}
        # go out of our way to make sure we make as many sibling layers as possible
        # even if one or more time steps are missing
        # NOTE: This does not handle if one product has a missing step and
        # another has a different missing time step
        time_master = max(namespace_siblings.values(), key=lambda v: len(v))
        for idx in range(len(time_master)):
            t = self[time_master[idx]][Info.SCHED_TIME]
            cs = self.channel_siblings(time_master[idx])
            channel_siblings = [(self[u][Info.SHORT_NAME], u) for u in cs[0]]
            temp_namespace = {}
            for sn, u in channel_siblings:
                if sn not in short_name_to_ns_name:
                    continue
                # set each ns variable to this UUID
                for ns_name in short_name_to_ns_name[sn]:
                    temp_namespace[ns_name] = u
            if len(temp_namespace) != len(namespace):
                LOG.info("Missing some layers to create algebraic layer at {:%Y-%m-%d %H:%M:%S}".format(t))
                continue
            LOG.info(
                "Creating algebraic layer '{}' for time {:%Y-%m-%d %H:%M:%S}".format(
                    info.get(Info.SHORT_NAME), self[time_master[idx]].get(Info.SCHED_TIME)
                )
            )

            uuid, layer_info, data = self._workspace.create_algebraic_composite(operations, temp_namespace, info.copy())
            self._layer_with_uuid[uuid] = dataset = DocBasicDataset(self, layer_info)
            presentation, reordered_indices = self._insert_layer_with_info(dataset, insert_before=insert_before)
            if Info.UNIT_CONVERSION not in dataset:
                dataset[Info.UNIT_CONVERSION] = units_conversion(dataset)
            if Info.FAMILY not in dataset:
                dataset[Info.FAMILY] = self.family_for_product_or_layer(dataset)
            self._add_layer_family(dataset)
            self.didAddCompositeDataset.emit(reordered_indices, dataset.uuid, presentation)

    def available_rgb_components(self):
        non_rgb_classes = [DocBasicDataset, DocCompositeDataset]
        valid_ranges = {}
        for layer in self.layers_where(is_valid=True, in_type_set=non_rgb_classes):
            sname = layer.get(Info.CENTRAL_WAVELENGTH, layer[Info.DATASET_NAME])
            valid_ranges.setdefault(sname, layer[Info.VALID_RANGE])
        return valid_ranges

    def _directory_of_layers(self, kind=Kind.IMAGE):
        if not isinstance(kind, (list, tuple)):
            kind = [kind]
        for x in [q for q in self._layer_with_uuid.values() if q.kind in kind]:
            yield x.uuid, x.sched_time, x.product_family_key

    def filter_active_layers(self, uuids):
        """Trim a sequence of product uuids to only the ones loaded in document"""
        docset = set(x.uuid for x in self._layer_with_uuid.values())
        for uuid in uuids:
            if uuid in docset:
                yield uuid

    def __len__(self):
        # FIXME: this should be consistent with __getitem__, not self.current_layer_set
        return len(self.current_layer_set)

    def uuid_for_current_layer(self, row):
        uuid = self.current_layer_set[row].uuid
        return uuid

    def family_uuids(self, family, active_only=False):
        uuids = self._families[family]
        if not active_only:
            return uuids
        current_visible = list(self.current_visible_layer_uuids)
        return [u for u in uuids if u in current_visible]

    def family_uuids_for_uuid(self, uuid, active_only=False):
        return self.family_uuids(self[uuid][Info.FAMILY], active_only=active_only)

    def remove_layers_from_all_sets(self, uuids):
        # find RGB layers family
        all_uuids = set()
        for uuid in list(uuids):
            all_uuids.add(uuid)
            if isinstance(self[uuid], DocRGBDataset):
                all_uuids.update(self.family_uuids_for_uuid(uuid))

        # collect all times for these layers to update RGBs later
        times = [self[u][Info.SCHED_TIME] for u in all_uuids]

        # delete all these layers from the layer list/presentation
        for uuid in all_uuids:
            LOG.debug("removing {} from family and Presentation lists".format(uuid))
            # remove from available family layers
            self._remove_layer_from_family(uuid)
            # remove from the layer set
            self.remove_layer_prez(uuid)  # this will send signal and start purge
            # remove from data layer collection
            if self.data_layer_collection is not None:
                self.data_layer_collection.remove_by_uuid(uuid)

        # Remove this layer from any RGBs it is a part of
        self.sync_composite_layer_prereqs(times)

        # Remove data from the workspace
        self.purge_layer_prez(uuids)

    def get_uuids(self):
        return list(self._layer_with_uuid.keys())

    def clear(self):
        """
        Convenience function to clear document
        """
        all_uuids = self.get_uuids()
        self.remove_layers_from_all_sets(all_uuids)

    def animate_siblings_of_layer(self, row_or_uuid):
        uuid = self.current_layer_set[row_or_uuid].uuid if not isinstance(row_or_uuid, UUID) else row_or_uuid
        layer = self._layer_with_uuid[uuid]
        L = self.current_layer_set

        if isinstance(layer, DocRGBDataset):
            pass
        else:
            new_anim_uuids, _ = self.time_siblings(uuid)
            if new_anim_uuids is None or len(new_anim_uuids) < 2:
                LOG.info("no time siblings to chosen band, will try channel siblings to chosen time")
                new_anim_uuids, _ = self.channel_siblings(uuid)
            if new_anim_uuids is None or len(new_anim_uuids) < 2:
                LOG.info("No animation found")
                return []

        LOG.debug("new animation order will be {0!r:s}".format(new_anim_uuids))
        L.animation_order = new_anim_uuids
        self.didReorderAnimation.emit(tuple(new_anim_uuids))
        return new_anim_uuids

    def get_info(self, row: int = None, uuid: UUID = None) -> typ.Optional[DocBasicDataset]:
        if row is not None:
            uuid_temp = self.current_layer_set[row].uuid
            nfo = self._layer_with_uuid[uuid_temp]
            return nfo
        elif uuid is not None:
            nfo = self._layer_with_uuid[uuid]
            return nfo
        return None

    def get_algebraic_namespace(self, uuid):
        return self._workspace.get_algebraic_namespace(uuid)

    def __getitem__(self, layer_uuid):
        """
        return layer with the given UUID
        """
        if layer_uuid is None:
            raise KeyError("Key 'None' does not exist in document or workspace")
        elif not isinstance(layer_uuid, UUID):
            raise ValueError("document[UUID] required, %r was used" % type(layer_uuid))

        if layer_uuid in self._layer_with_uuid:
            return self._layer_with_uuid[layer_uuid]

        # check the workspace for information
        try:
            LOG.debug("Checking workspace for information on inactive product")
            info = self._workspace.get_info(layer_uuid)
        except KeyError:
            info = None

        if info is None:
            raise KeyError("Key '{}' does not exist in document or workspace".format(layer_uuid))
        return info

    def reorder_by_indices(self, new_order, uuids=None, layer_set_index=None):
        """given a new layer order, replace the current layer set
        emits signal to other subsystems
        """
        if layer_set_index is None:
            layer_set_index = self.current_set_index
        assert len(new_order) == len(self._layer_sets[layer_set_index])
        new_layer_set = DocLayerStack(self, [self._layer_sets[layer_set_index][n] for n in new_order])
        self._layer_sets[layer_set_index] = new_layer_set
        self.didReorderDatasets.emit(tuple(new_order))

    def insert_layer_prez(self, row: int, layer_prez_seq):
        cls = self.current_layer_set
        clo = list(range(len(cls)))
        lps = list(layer_prez_seq)
        lps.reverse()
        if not lps:
            LOG.warning("attempt to drop empty content")
            return
        for p in lps:
            if not isinstance(p, Presentation):
                LOG.error("attempt to drop a new layer with the wrong type: {0!r:s}".format(p))
                continue
            cls.insert(row, p)
            clo.insert(row, None)

    def is_using(self, uuid: UUID, layer_set: int = None):
        "return true if this dataset is still in use in one of the layer sets"
        layer = self._layer_with_uuid[uuid]
        if layer_set is not None:
            lss = [self._layer_sets[layer_set]]
        else:
            lss = [q for q in self._layer_sets if q is not None]
        for ls in lss:
            for p in ls:
                if p.uuid == uuid:
                    return True
                parent_layer = self._layer_with_uuid[p.uuid]
                if parent_layer.kind == Kind.RGB and layer in parent_layer.layers:
                    return True
        return False

    def remove_layer_prez(self, row_or_uuid, count: int = 1):
        """
        remove the presentation of a given layer/s in the current set
        :param row: which current layer set row to remove
        :param count: how many rows to remove
        :return:
        """
        if isinstance(row_or_uuid, UUID) and count == 1:
            try:
                row = self.row_for_uuid(row_or_uuid)
            except KeyError:
                LOG.debug("Can't remove in-active layer: {}".format(row_or_uuid))
                return
            uuids = [row_or_uuid]
        else:
            row = row_or_uuid
            uuids = [x.uuid for x in self.current_layer_set[row : row + count]]
        self.toggle_layer_visibility(list(range(row, row + count)), False)
        clo = list(range(len(self.current_layer_set)))
        del clo[row : row + count]
        del self.current_layer_set[row : row + count]
        self.didRemoveDatasets.emit(tuple(clo), uuids, row, count)

    def purge_layer_prez(self, uuids):
        """Purge layers from the workspace"""
        for uuid in uuids:
            if not self.is_using(uuid):
                LOG.debug("purging layer {}, no longer in use".format(uuid))
                self.willPurgeDataset.emit(uuid)
                # remove from our bookkeeping
                del self._layer_with_uuid[uuid]
                # remove from workspace
                self._workspace.remove(uuid)

    def channel_siblings(self, uuid, sibling_infos=None):
        """
        filter document info to just dataset of the same channels
        meaning all channels at a specific time, in alphabetical name order
        :param uuid: focus UUID we're trying to build around
        :param sibling_infos: dictionary of UUID -> Dataset Info to sort through
        :return: sorted list of sibling uuids in channel order
        """
        if sibling_infos is None:
            sibling_infos = self._layer_with_uuid
        it = sibling_infos.get(uuid, None)
        if it is None:
            return None
        sibs = [
            (x[Info.SHORT_NAME], x[Info.UUID])
            for x in self._filter(
                sibling_infos.values(), it, {Info.SCENE, Info.SCHED_TIME, Info.INSTRUMENT, Info.PLATFORM}
            )
        ]
        # then sort it by bands
        sibs.sort()
        offset = [i for i, x in enumerate(sibs) if x[1] == uuid]
        return [x[1] for x in sibs], offset[0]

    def _filter(self, seq, reference, keys):
        "filter a sequence of metadata dictionaries to matching keys with reference"
        for md in seq:
            fail = False
            for key in keys:
                val = reference.get(key, None)
                v = md.get(key, None)
                if val != v:
                    fail = True
            if not fail:
                yield md

    def get_unique_product_family_keys(self) -> typ.List[typ.Tuple]:
        """
        Return list of unique product_family_keys in i.e. document._layer_with_uuid's
        DocBasicDatasets.
        :return: unique_product_family_keys
        """
        prod_fam_keys = [dbl.product_family_key for dbl in self._layer_with_uuid.values()]
        unique_product_family_keys = []
        for pf_key in prod_fam_keys:
            if pf_key in unique_product_family_keys:
                continue
            unique_product_family_keys.append(pf_key)
        return unique_product_family_keys

    def create_data_layers(self) -> typ.List[DataLayer]:
        """
        Adds data layers to document, based on currently present
        uuid -> DocBasicDataset (document._layer_with_uuid) mappings.
        :return: data_layers: typ.List of data layers to be incorporated into DataLayerCollection.
        """
        pf_keys: typ.List[typ.Tuple] = [dbl.product_family_key for dbl in self._layer_with_uuid.values()]
        unique_pf_keys = set(pf_keys)
        data_layers: typ.List[DataLayer] = []
        for curr_pf_key in unique_pf_keys:
            curr_pf_uuids_timestamps: typ.List[typ.Tuple] = []
            for image_uuid, doc_basic_layer in self._layer_with_uuid.items():
                if doc_basic_layer.product_family_key != curr_pf_key:
                    continue
                curr_pf_uuids_timestamps.append((image_uuid, doc_basic_layer.sched_time))
            data_layers.append(DataLayer(curr_pf_uuids_timestamps, curr_pf_key))
        return data_layers

    def time_siblings(self, uuid, sibling_infos=None):
        """
        return time-ordered list of datasets which have the same band, in time order
        :param uuid: focus UUID we're trying to build around
        :param sibling_infos: dictionary of UUID -> Dataset Info to sort through
        :return: sorted list of sibling uuids in time order, index of where uuid is in the list
        """
        # NOTE(mk): create_data_layers is the bridge between document and collection
        # as it uses state from document to create a list of data_layers that can then
        # be passed to methods of data_layer_collection
        # self.create_data_layers()
        # TODO(mk) pull create_data_layers into DataLayerCollection
        # self.data_layer_collection = DataLayerCollection(self.data_layers)
        # self.data_layer_collection.notify_extend_collection(self.data_layers)
        if sibling_infos is None:
            sibling_infos = self._layer_with_uuid
        it = sibling_infos.get(uuid, None)
        if it is None:
            return [], 0
        sibs = [
            (x[Info.SCHED_TIME], x[Info.UUID])
            for x in self._filter(
                sibling_infos.values(),
                it,
                {Info.SHORT_NAME, Info.STANDARD_NAME, Info.SCENE, Info.INSTRUMENT, Info.PLATFORM, Info.KIND},
            )
        ]
        # then sort it into time order
        sibs.sort()
        offset = [i for i, x in enumerate(sibs) if x[1] == uuid]
        return [x[1] for x in sibs], offset[0]

    def time_siblings_uuids(self, uuids, sibling_infos=None):
        """
        return generator uuids for datasets which have the same band as the uuids provided
        :param uuids: iterable of uuids
        :param infos: list of dataset infos available, some of which may not be relevant
        :return: generate sorted list of sibling uuids in time order and in provided uuid order
        """
        for requested_uuid in uuids:
            for sibling_uuid in self.time_siblings(requested_uuid, sibling_infos=sibling_infos)[0]:
                yield sibling_uuid


#
# class DocumentTreeBranch(QObject):
#     pass
#
# class DocumentTreeLeaf(QObject):
#     pass
#
#
# class DocumentAsLayerTree(QObject):
#     """
#      DocumentAsLayerTree is a facet or wrapper (if it were a database, it would be a view; but view is already taken)
#      It allows the layer controls - specifically a LayerStackTreeViewModel - to easily access and modify
#      the document on behalf of the user.
#      It includes both queries for display and changes which then turn into document updates
#      The base model is just a list of basic layers.
#      Composite and Algebraic layers, however, are more like folders.
#      Other additional layer types may also have different responses to being dragged or having items dropped on them
#     """
#
#     def __init__(self, doc, *args, **kwargs):
#         self._doc = doc
#         super(DocumentAsLayerTree, self).__init__()
#
#     def
#
