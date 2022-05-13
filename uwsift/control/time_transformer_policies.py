import logging
from datetime import datetime
from typing import List, Optional

import numpy as np
from PyQt5.QtCore import QObject, pyqtSignal

from uwsift.model.layer_item import LayerItem

LOG = logging.getLogger(__name__)


class WrappingDrivingPolicy(QObject):
    """
    Time translation policy that when called returns a simulated data time
    by taking a timestamp from a designated data layer (the driving layer).
    Each successive call will return the driving layer's next timestamp. If there are
    no more timestamps in the driving layer it starts over from the first timestamp.
    """

    didUpdatePolicy = pyqtSignal(LayerItem)

    def __init__(self, layers: List[LayerItem]):
        super().__init__()
        self._layers: List[LayerItem] = layers
        self._driving_idx = 0
        self._curr_t_sim = None
        self._timeline = None
        self._driving_layer = None
        self._driving_layer_uuid = None

    def _get_dynamic_layers(self):
        return [layer for layer in self._layers if layer.dynamic]

    def _driving_layer_index_in_layers(self) -> Optional[int]:
        try:
            idx = self._get_dynamic_layers().index(self._driving_layer_uuid)
            return idx
        except ValueError:
            return None

    def get_next_possible_driving_layer(self) -> Optional[LayerItem]:
        for layer in self._get_dynamic_layers():
            return layer
        LOG.info("No suitable driving layer found!")
        return None

    def on_layers_update(self):
        """
        Slot connected to LayerModel's 'didUpdateLayers' signal. Takes the first loaded layer, of
        a suitable kind, if the old driving layer is not in LayerModel's layers anymore.
        """
        driving_layer_index = self._driving_layer_index_in_layers()
        if driving_layer_index:
            return
            # self.driving_layer = self._layers[driving_layer_index]
        else:
            self.driving_layer = self.get_next_possible_driving_layer()
            self.didUpdatePolicy.emit(self.driving_layer)

    def change_timebase(self, layer):
        self.driving_layer = layer
        self.didUpdatePolicy.emit(layer)

    @property
    def timeline_length(self):
        return len(self._timeline)

    @property
    def driving_layer(self):
        return self._driving_layer

    @driving_layer.setter
    def driving_layer(self, layer: LayerItem):
        if not layer or not layer.dynamic:
            self._driving_layer = None
            # raise ValueError("No valid Driving layer")
        elif not self._driving_layer:
            self._driving_layer = layer
            self.timeline = list(self._driving_layer.timeline.keys())

            self._driving_idx = 0
        else:
            # Retrieve  time step of new timeline analogous to previous
            # simulation time (stored in self._current_t_sim).
            self.timeline = list(layer.timeline.keys())
            self._driving_layer = layer
            nearest_past_idx = self._find_nearest_past(self._curr_t_sim)
            if nearest_past_idx:
                self._driving_idx = nearest_past_idx
            else:
                self._driving_idx = 0
        self._curr_t_sim = None if not self.timeline else self.timeline[self._driving_idx]

    @property
    def timeline(self):
        return self._timeline

    @timeline.setter
    def timeline(self, timeline: List[datetime]):
        self._timeline = timeline

    def _find_nearest_past(self, tstamp: datetime) -> Optional[int]:
        """
        Upon driving layer change find the nearest past tstamp in the new driving
        layer and return its index.
        """
        old_tstamp_np = np.asarray([tstamp])
        other_timeline_np = np.asarray(self.timeline)
        past_idcs = other_timeline_np <= old_tstamp_np
        distances = np.abs(other_timeline_np[past_idcs] - old_tstamp_np)
        if distances.size > 0:
            return np.argmin(distances)
        else:
            return None

    def curr_t_sim(self):
        if not self.timeline:
            assert self._curr_t_sim is None
        else:
            assert self._curr_t_sim == self.timeline[self._driving_idx]
        return self._curr_t_sim

    def curr_timeline_index(self):
        return self._driving_idx

    def jump_to_t_sim(self, index: int) -> datetime:
        """
            Returns t_sim by looking up the driving layer's timestamp at the provided index
            location. Raises an exception if being called with an invalid index.
        :param index: integer location in the timeline to jump to.
        :return: datetime object located at the provided index.
        """
        try:
            self._driving_idx = index
            t_sim = self.timeline[self._driving_idx]
        except Exception as e:
            LOG.error(f"Invalid index passed to driving layer timeline: " f"index={index}\n", exc_info=True)
            raise e
        return t_sim

    def compute_t_sim(self, tick_time: int, backwards: bool = False) -> datetime:
        """
        Returns timestamp t_sim by:

            1) backwards set to False and index is None:
               incrementing the index of the current driving layer and
               returning the timestamp of the driving layer timeline at the
               index location

            2) backwards set to True and index is None:
               decrementing the index of the current driving layer...(see 1)

            3) backwards set to False but providing setting the index kwarg
               to a valid integer:
               looking up the driving layer's timestamp at the provided index
               location
        """
        if backwards:
            self._driving_idx = (self._driving_idx + (self.timeline_length - 1)) % self.timeline_length
        else:
            self._driving_idx += 1
            if self._driving_idx >= self.timeline_length:
                self._driving_idx = 0
        self._curr_t_sim = self.timeline[self._driving_idx]
        return self._curr_t_sim
