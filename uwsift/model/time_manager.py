import logging
from typing import Optional

from PyQt5.QtCore import QDateTime, QObject, pyqtSignal

from uwsift.control.time_matcher import TimeMatcher
from uwsift.control.time_matcher_policies import find_nearest_past
from uwsift.control.time_transformer import TimeTransformer
from uwsift.control.time_transformer_policies import WrappingDrivingPolicy
from uwsift.control.qml_utils import QmlLayerManager, TimebaseModel, QmlBackend
from datetime import datetime
from dateutil.relativedelta import relativedelta

from uwsift.model.layer_item import LayerItem
from uwsift.model.layer_model import LayerModel

LOG = logging.getLogger(__name__)


class TimeManager(QObject):
    # TODO(mk): make this class abstract and subclass,
    #           as soon as non driving layer policies are necessary?
    """
        Actions upon tick event:
            - Time Manager gets t_sim from t2t_translator
            - forwards it to Display Layers
            - Display Layers each give their timeline and t_sim to TimeMatcher
            - TimeMatcher returns t_matched for every non-driving layer timeline
            - each Display Layer requests the image corresponding to the matched timestamp
              from collection
            - Image is displayed
    """

    didMatchTimes = pyqtSignal(dict)

    def __init__(self, animation_speed, matching_policy=find_nearest_past):
        super().__init__()
        self._animation_speed = animation_speed
        self._time_matcher = TimeMatcher(matching_policy)

        self._layer_model: Optional[LayerModel] = None

        self.qml_root_object = None
        self.qml_engine = None
        self._qml_backend = None
        self.qml_layer_manager: QmlLayerManager = QmlLayerManager()

        dummy_dt = datetime.now()
        dummy_dt = datetime(dummy_dt.year, dummy_dt.month, dummy_dt.day, dummy_dt.hour)
        test_qdts = list(map(lambda dt: QDateTime(dt),
                             [dummy_dt + relativedelta(hours=i) for i in range(5)]))
        self.qml_timestamps_model = TimebaseModel(timestamps=test_qdts)
        self._time_transformer: Optional[TimeTransformer] = None

    @property
    def qml_backend(self) -> QmlBackend:
        return self._qml_backend

    @qml_backend.setter
    def qml_backend(self, backend):
        self._qml_backend = backend

    def connect_to_model(self, layer_model: LayerModel):
        self._layer_model: LayerModel = layer_model
        self.qml_layer_manager._layer_model = layer_model

        policy = WrappingDrivingPolicy(self._layer_model.layers)
        layer_model.didUpdateLayers.connect(policy.on_layers_update)
        layer_model.didUpdateLayers.connect(self.update_qml_layer_model)
        layer_model.didUpdateLayers.connect(self.sync_to_time_transformer)

        self.didMatchTimes.connect(self._layer_model.on_didMatchTimes)

        policy.didUpdatePolicy.connect(self.update_qml_timeline)
        self._time_transformer = TimeTransformer(policy)

    def tick(self, event):
        """
        Proxy function for `TimeManager.step()`, which cannot directly
        receive a signal from the animation timer signal because the latter
        passes an `event` that `step()` cannot deal with. Thus connect to
        this method to actually trigger `step()`.

        :param event: Event passed by `AnimationController.animation_timer` on
        expiry, simply dropped.
        """
        self.step()

    def step(self, backwards: bool = False):
        """
        Method allowing advancement of time, either forwards or backwards, by
        one time step, as determined by the currently active time base.

        :param backwards: Flag which sets advancement either to `forwards` or
        `backwards`.
        """
        self._time_transformer.step(backwards=backwards)
        self.sync_to_time_transformer()

    def jump(self, index):
        self._time_transformer.jump(index)
        self.sync_to_time_transformer()

    def sync_to_time_transformer(self):
        t_sim = self._time_transformer.t_sim
        t_idx = self._time_transformer.timeline_index

        t_matched_dict = self._match_times(t_sim)
        self.didMatchTimes.emit(t_matched_dict)

        self.tick_qml_state(t_sim, t_idx)

    def _match_times(self, t_sim: datetime) -> dict:
        """
        Match time steps of available data in LayerModel's dynamic layers to
        `t_sim` of i.e.: a driving layer.

        A mapping of one layer to multiple soon-to-be visible ProductDatasets is
        made possible to support products (i.e.: Lightning) where multiple
        ProductDatasets may accumulate and must thus be made visible to the
        user.

        :param t_sim: Datetime of current active time step of time base.
        :return: Dictionary of possibly multiple tuples of
        (layer_uuid -> [product_dataset_uuid0,..,product_dataset_uuidN]),
        describing all ProductDatasets within a layer that are to be set
        visible.
        """
        t_matched_dict = {}
        for layer in self._layer_model.get_dynamic_layers():
            t_matched = self._time_matcher.match(layer.timeline, t_sim)
            if t_matched:
                t_matched_dict[layer.uuid] = [layer.timeline[t_matched].uuid]
            else:
                t_matched_dict[layer.uuid] = [None]
        return t_matched_dict

    def update_qml_timeline(self, layer: LayerItem):
        """
        Slot that updates and refreshes QML timeline state using a DataLayer that is either:
            a) a driving layer or some other form of high priority data layer
            b) a 'synthetic' data layer, only created to reflect the best fitting
                timeline/layer info for the current policy -> this may be policy-dependant
            # TODO(mk): the policy should not be responsible for UI, another policy or an object
                        that ingests a policy and handles UI based on that?
        """
        if not layer or not layer.dynamic:
            return
        if not self._time_transformer.t_sim:
            self.qml_timestamps_model.currentTimestamp = list(layer.timeline.keys())[0]
        else:
            self.qml_timestamps_model.currentTimestamp = self._time_transformer.t_sim

        self.qml_engine.clearComponentCache()

        new_timestamp_qdts = list(map(lambda dt: QDateTime(dt), layer.timeline.keys()))
        self.qml_timestamps_model.timestamps = new_timestamp_qdts
        self.qml_backend.refresh_timeline()

    def update_qml_layer_model(self):
        """
        Slot connected to didUpdateCollection signal, responsible for
        managing the data layer combo box contents
        """
        dynamic_layers_descriptors = []
        for layer in self._layer_model.get_dynamic_layers():
            dynamic_layers_descriptors.append(layer.descriptor)

        self.qml_layer_manager._qml_layer_model.layer_strings = \
            dynamic_layers_descriptors
        # TODO(mk): create cleaner interface to get timebase index, should not directly
        #           access policy, expose via transformer?
        time_index = self._time_transformer._translation_policy._driving_idx
        self.qml_backend.didChangeTimebase.emit(time_index)

    def tick_qml_state(self, t_sim, timeline_idx):
        # TODO(mk): if TimeManager is subclassed the behavior below must be adapted:
        #           it may no longer be desirable to show t_sim as the current time step
        self.qml_timestamps_model.currentTimestamp = self._time_transformer.t_sim
        self.qml_backend.doNotifyTimelineIndexChanged.emit(timeline_idx)

    def create_formatted_t_sim(self):
        """
        Used for updating the animation label during animation.
        """
        return self._time_transformer.create_formatted_time_stamp()

    def on_timebase_change(self, index):
        """
        Slot to trigger timebase change by looking up data layer at specified
        index. Then calls time transformer to execute change of the timebase.

        :param index: DataLayer index obtained by either: clicking an item in
                      the ComboBox or by clicking a convenience function in the
                      convenience function popup menu
        """
        layer = self._layer_model.get_dynamic_layers()[index]
        if layer:
            self._time_transformer.change_timebase(layer)
            self.update_qml_timeline(layer)
            self.qml_backend.refresh_timeline()
            self.sync_to_time_transformer()
