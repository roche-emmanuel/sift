import logging
import struct
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from PyQt5.QtCore import QAbstractItemModel, QMimeData, QModelIndex, Qt, pyqtSignal

from uwsift.common import (
    BORDERS_DATASET_NAME,
    LATLON_GRID_DATASET_NAME,
    LAYER_TREE_VIEW_HEADER,
    Info,
    Instrument,
    Kind,
)
from uwsift.common import LayerModelColumns as LMC
from uwsift.common import LayerVisibility, Platform, Presentation
from uwsift.model import Document
from uwsift.model.composite_recipes import AlgebraicRecipe, CompositeRecipe, Recipe
from uwsift.model.layer_item import LayerItem
from uwsift.model.product_dataset import ProductDataset
from uwsift.workspace.workspace import frozendict

LOG = logging.getLogger(__name__)


class LayerModel(QAbstractItemModel):
    # ------------------- Creating layers and product datasets -----------------
    didCreateLayer = pyqtSignal(LayerItem)

    didAddCompositeDataset = pyqtSignal(LayerItem, ProductDataset)
    didAddImageDataset = pyqtSignal(LayerItem, ProductDataset)
    didAddLinesDataset = pyqtSignal(LayerItem, ProductDataset)
    didAddPointsDataset = pyqtSignal(LayerItem, ProductDataset)

    didAddImageLayer = pyqtSignal(LayerItem)
    didAddSystemLayer = pyqtSignal(LayerItem)

    # ------------------ Changing properties of existing layers ----------------
    didChangeColormap = pyqtSignal(dict)
    didChangeColorLimits = pyqtSignal(dict)
    didChangeGamma = pyqtSignal(dict)
    didChangeLayerVisible = pyqtSignal(UUID, bool)
    didChangeLayerOpacity = pyqtSignal(UUID, float)
    didChangeRecipeLayerNames = pyqtSignal()

    didUpdateLayers = pyqtSignal()
    didReorderLayers = pyqtSignal(list)
    didFinishActivateProductDatasets = pyqtSignal()
    # didChangeLayerName = pyqtSignal(UUID, str)  # layer uuid, new name

    # --------------- Adding layers derived from existing layers ---------------
    # didAddCompositeLayer = pyqtSignal(tuple, UUID, Presentation)

    # ----------------------- Removing existing layers -------------------------
    # didDeleteLayer = pyqtSignal(UUID)
    didDeleteProductDataset = pyqtSignal(UUID)
    # ---------------------- Request creation of Recipes -----------------------
    # object should be a List[Optional[UUID]]
    didRequestCompositeRecipeCreation = pyqtSignal(object)
    # object should be a List[Optional[UUID]]
    didRequestAlgebraicRecipeCreation = pyqtSignal(object)

    # --------------------------------------------------------------------------
    # didChangeImageKind = pyqtSignal(dict)

    didActivateProductDataset = pyqtSignal(UUID, bool)
    # The parameter here should be a list, but in some cases PyQt has problems
    # with this. If the list is only filled with Nones, then sending this
    # signal will cause a 139 error and the application will crash. But if
    # the parameter is of type object, then the crash does not occur if the
    # list is only filled with None. This is only a workaround and needs to be
    # fixed when the problem with PyQT no longer occurs.
    # See also:
    # https://stackoverflow.com/questions/12050397/pyqt-signal-emit-with-object-instance-or-none
    didChangeCompositeProductDataset = pyqtSignal(LayerItem, ProductDataset)

    didRequestSelectionOfLayer = pyqtSignal(QModelIndex)

    def __init__(self, document: Document, parent=None, policy=None):
        """
        Model for a "flat" layer tree (list/table of layers)
        (Note: For hierarchies the `parent` and `index` methods, among others,
        need rework).

        :param parent: Parent QObject of the LayerModel (Qt QObject hierarchy
                       argument)
        :param policy: Policy to match product datasets to layers and return a
                       matching layer.
        """

        super().__init__(parent)

        self._document = document
        self._workspace = self._document._workspace
        assert self._workspace  # Verify proper initialisation order

        self._headers = LAYER_TREE_VIEW_HEADER

        if policy is None:
            self.policy = ProductFamilyKeyMappingPolicy(model=self)
        else:
            self.policy = policy(model=self)

        self.layers: List[LayerItem] = []

        self._supportedRoles = [Qt.DisplayRole, Qt.EditRole, Qt.TextAlignmentRole]

    def _init_system_layer(self, name):
        # The minimal 'dataset' information required by LayerItem
        # initialization:
        pseudo_info = frozendict(
            {Info.KIND: Kind.LINES, Info.PLATFORM: Platform.SYSTEM, Info.INSTRUMENT: Instrument.GENERATED, "name": name}
        )

        presentation = Presentation(uuid=None, kind=Kind.LINES)

        system_layer = LayerItem(self, pseudo_info, presentation)

        self.didCreateLayer.emit(system_layer)
        self._add_layer(system_layer)
        self.didAddSystemLayer.emit(system_layer)

    def init_system_layers(self):
        """Create layers whose existence is controlled by the system, not
        by the user.

        Currently two system layers are set up, one for a latitude/longitude
        grid, the second for political borders.
        """
        for dataset_name in [LATLON_GRID_DATASET_NAME, BORDERS_DATASET_NAME]:
            self._init_system_layer(dataset_name)

    def get_dynamic_layers(self):
        return [layer for layer in self.layers if layer.dynamic]

    def data(self, index: QModelIndex, role: int = None):
        if not index.isValid():
            return None
        if role not in self._supportedRoles:
            return None

        if role == Qt.DisplayRole:
            layer: LayerItem = self.layers[index.row()]
            return layer.data(index.column())

        if role == Qt.TextAlignmentRole:
            if index.column() in [LMC.WAVELENGTH, LMC.PROBE_VALUE]:
                return Qt.AlignRight
            return Qt.AlignLeft

        raise NotImplementedError(f"Missing implementation for supported" f" Qt.ItemDataRole {role}")

    def flags(self, index):
        if index.isValid():
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        else:
            flags = Qt.ItemIsDropEnabled
        return flags

    def supportedDropActions(self):
        return Qt.MoveAction

    def headerData(self, section: int, orientation, role=None):

        if (orientation == Qt.Horizontal) and (role == Qt.DisplayRole):
            return self._headers[section]
        return None

    def _add_layer(self, layer: LayerItem) -> None:
        """
        Insert given layer into the LayerModel's `layers` list.

        :param layer: Layer to be inserted into LayerModel.

        The position of the new layer in the layer stack is determined by the
        kind of the layer: Layers of kinds LINES and POINTS are put in front,
        layers of other kinds are placed in front of the first existing layer,
        which is also neither of kind LINES nor POINTS.

        Emits the didReorderLayers() signal and (indirectly) other
        QAbstractItemModel signals notifying about the model change.
        """
        if layer in self.layers:
            raise ValueError(f"LayerItem {layer} is already in LayerModel.")

        # Determine the row to put the new layer into: layers of kinds POINTS
        # and LINES (for now) should be put on top of all existing layers (be
        # "favoured"), others should be on top of the existing layers of the
        # other kinds but below layers of a "favoured" kind.
        favoured_kinds = [
            Kind.LINES,
            Kind.POINTS,
        ]

        row = 0
        if layer.kind not in favoured_kinds:
            row = self.rowCount()  # fallback position: append
            for idx, existing_layer in enumerate(self.layers):
                if existing_layer.kind not in favoured_kinds:
                    # Found the first layer not of favoured kind: insert new
                    # layer right here putting it in front of the found one.
                    row = idx
                    break

        self.beginInsertRows(QModelIndex(), row, row)
        self.layers.insert(row, layer)
        self.endInsertRows()

        if layer.kind == Kind.IMAGE:
            self.didAddImageLayer.emit(layer)

        self._emit_didReorderLayers()

    def _emit_didReorderLayers(self):  # noqa
        uuids = [layer.uuid for layer in self.layers]
        self.didReorderLayers.emit(uuids)

    def hasChildren(self, parent=QModelIndex()) -> bool:
        """
        For now the Layer model does not support layer hierarchies
        (group layers) thus only the root index can have children.

        :param parent: model index to query
        :return: true if parent is the root index and has at least one row and
                 column
        """
        # This needs modification if hierarchical layers are introduced.
        return not parent.isValid() and (self.rowCount(parent) > 0) and (self.columnCount(parent) > 0)

    def columnCount(self, parent=None):
        return len(self._headers)

    def rowCount(self, parent=None):
        # This needs modification if hierarchical layers are introduced.
        return len(self.layers)

    def index(self, row: int, col: int, parent=QModelIndex()):
        if not self.hasIndex(row, col, parent):
            return QModelIndex()
        assert not parent.isValid()
        layer = self.layers[row]
        return self.createIndex(row, col, layer)

    def parent(self, index):
        # This needs modification if hierarchical layers are introduced.
        return QModelIndex()

    def order(self, layer: LayerItem) -> int:
        """
        Method to return the order of a specific layer within the model.
        Determined by its index in the model.

        :param layer: Layer whose oorder is queried.
        :return: Integer representing the order of queried layer.
        """
        return self.layers.index(layer)

    def setData(self, index: QModelIndex, data, role: int = Qt.EditRole):
        if not index.isValid():
            return False

        assert role == Qt.EditRole, f"Unexpected role {role} for changing data."
        assert index.column() == LMC.VISIBILITY, f"Attempt to edit immutable column {index.column()}."

        LOG.debug(f"Changing row {index.row()}, column {index.column()}" f" to {data}.")

        layer = self.layers[index.row()]
        layer_visibility: LayerVisibility = data
        if layer.opacity != layer_visibility.opacity:
            layer.opacity = layer_visibility.opacity
            LOG.debug(f"Layer opacity changed to:" f" {self.layers[index.row()].opacity}")
            self.didChangeLayerOpacity.emit(layer.uuid, layer.opacity)
        if layer.visible != layer_visibility.visible:
            layer.visible = layer_visibility.visible
            LOG.debug(f"Layer visible changed to:" f" {self.layers[index.row()].visible}")
            self.didChangeLayerVisible.emit(layer.uuid, layer.visible)
        self.dataChanged.emit(index, index)
        return True

    def get_layer_by_uuid(self, uuid: UUID) -> Optional[LayerItem]:
        layers = [layer for layer in self.layers if layer.uuid == uuid]
        if len(layers) > 1:
            raise ValueError(
                f"Multiple Layers with UUID: {uuid} found" f" with product_family_key:" f" {self.product_family_key}!"
            )
        elif len(layers) == 0:
            return None
        else:
            return layers[0]

    def _get_layer_for_dataset(self, info: frozendict, presentation: Presentation) -> LayerItem:
        layer, grouping_key = self.policy.get_existing_layer_for_dataset(info)

        if not layer:
            # According to the policy, there is no layer for the dataset
            # represented by the given info yet, so a new one has to be
            # created. We use the grouping_key returned by the policy to
            # ensure that the new layer is compatible with the dataset in
            # terms of the policy.
            layer = LayerItem(self, info, presentation, grouping_key)
            self.didCreateLayer.emit(layer)
            self._add_layer(layer)

        return layer

    def add_dataset(self, info: frozendict, presentation: Presentation) -> None:
        """
        Slot specifically to fill model from Document's
        `activate_product_uuid_as_new_layer`.
        For every loaded dataset Document emits `didAddDataset` signal which
        must be connected to this method.

        :param info: Dictionary of dataset metadata information.
        :param presentation: Presentation to be set for layer, when a new one
               has to be created to hold the dataset, ignored otherwise.
        """
        layer: LayerItem = self._get_layer_for_dataset(info, presentation)

        # Note: For now we do not set a presentation *per dataset*. In the
        #  future we may think about doing this, but there is not yet an
        #  according control flow that could be chosen by the user.
        product_dataset = layer.add_dataset(info)
        if product_dataset is not None:
            if product_dataset.kind == Kind.IMAGE:
                self.didAddImageDataset.emit(layer, product_dataset)
            elif product_dataset.kind == Kind.LINES:
                self.didAddLinesDataset.emit(layer, product_dataset)
            elif product_dataset.kind == Kind.POINTS:
                self.didAddPointsDataset.emit(layer, product_dataset)
            else:
                raise NotImplementedError(f"Managing datasets of kind {product_dataset.kind}" f" not (yet) supported.")

            self.didUpdateLayers.emit()
        self._trigger_composite_layer_update(layer)

    def mimeTypes(self):
        return ["text/plain", "text/xml"]

    def mimeData(self, indexes):
        mime_data = QMimeData()
        rows = list(set([index.row() for index in indexes]))
        row_bytes = struct.pack("<I", rows[0])
        mime_data.setData("text/plain", row_bytes)
        return mime_data

    def dropMimeData(self, mime_data, action, row, column, parentIndex):
        if action == Qt.IgnoreAction:
            return True
        if action != Qt.MoveAction:
            return False

        source_row = struct.unpack("<I", mime_data.data("text/plain"))[0]

        if row != -1:  # we may also interpret this as put to the end
            target_row = row
        elif parentIndex.isValid():
            assert not parentIndex.isValid(), (
                "BUG: hierarchical layers not implemented," " dropping on a parent must not yet occur!"
            )
            # This case needs modification when hierarchical layers are
            # introduced.
            target_row = parentIndex.row()  # just to keep the linter calm
        else:
            target_row = self.rowCount(QModelIndex())

        move_is_possible = self.beginMoveRows(QModelIndex(), source_row, source_row, parentIndex, target_row)
        if not move_is_possible:
            return False

        # According to https://doc.qt.io/qt-5/qabstractitemmodel.html#beginMoveRows
        # now we can assert ...
        assert not source_row <= target_row <= source_row + 1

        if source_row < target_row:
            target_row -= 1
        self.layers.insert(target_row, self.layers.pop(source_row))
        self.endMoveRows()

        self._emit_didReorderLayers()
        self._refresh()

        return True

    def _refresh(self):
        self.layoutAboutToBeChanged.emit()
        self.revert()
        self.layoutChanged.emit()

    def on_didMatchTimes(self, t_matched_dict: dict):  # noqa
        for layer_uuid, active_dataset_uuids in t_matched_dict.items():
            layer = self.get_layer_by_uuid(layer_uuid)
            for product_dataset in layer.timeline.values():
                if product_dataset.uuid in active_dataset_uuids:
                    product_dataset.is_active = True
                else:
                    product_dataset.is_active = False
                self.didActivateProductDataset.emit(product_dataset.uuid, product_dataset.is_active)
        self.didFinishActivateProductDatasets.emit()

    def get_probeable_layers(self) -> List[LayerItem]:
        """Get LayerItems which may contain data suitable for probing
        operations.

        Currently only single channel raster data can be point or region probed,
        thus the layer must be one capable of carrying datasets of kind IMAGE or
        COMPOSITE.
        """
        return [layer for layer in self.layers if layer.kind in [Kind.IMAGE, Kind.COMPOSITE]]

    def get_top_probeable_layer(self) -> Optional[LayerItem]:
        probeable_layers = self.get_probeable_layers()
        return None if len(probeable_layers) == 0 else probeable_layers[0]

    def get_top_probeable_layer_with_active_product_dataset(
        self,
    ) -> Tuple[Optional[LayerItem], Optional[ProductDataset]]:
        top_probeable_layer = self.get_top_probeable_layer()
        return (
            (None, None)
            if top_probeable_layer is None
            else (top_probeable_layer, top_probeable_layer.get_first_active_product_dataset())
        )

    @staticmethod
    def _build_presentation_change_dict(layer: LayerItem, presentation_element: object):
        product_datasets_uuids = layer.get_datasets_uuids()
        return {pd_uuid: presentation_element for pd_uuid in product_datasets_uuids}

    def change_colormap_for_layer(self, uuid: UUID, colormap: object):
        layer = self.get_layer_by_uuid(uuid)
        layer.presentation.colormap = colormap
        change_dict = self._build_presentation_change_dict(layer, colormap)
        self.didChangeColormap.emit(change_dict)

    def change_color_limits_for_layer(self, uuid: UUID, color_limits: object):
        layer = self.get_layer_by_uuid(uuid)
        layer.presentation.climits = color_limits
        change_dict = self._build_presentation_change_dict(layer, color_limits)
        self.didChangeColorLimits.emit(change_dict)

    def change_gamma_for_layer(self, uuid: UUID, gamma: float):
        layer = self.get_layer_by_uuid(uuid)
        layer.presentation.gamma = gamma
        change_dict = self._build_presentation_change_dict(layer, gamma)
        self.didChangeGamma.emit(change_dict)

    def on_point_probe_set(self, probe_name, state, xy_pos, uuids=None):
        """user has clicked on a point probe; determine relative and absolute
        values for all document image layers
        """
        if not state:
            for layer in self.get_probeable_layers():
                layer.probe_value = None
        else:
            for layer in self.get_probeable_layers():
                product_dataset = layer.get_first_active_product_dataset()

                layer.probe_value = (
                    None if not product_dataset else self._workspace.get_content_point(product_dataset.uuid, xy_pos)
                )

        self._refresh()

    def create_rgb_composite_layer(self, recipe: CompositeRecipe):
        """Creates a layer which has a rgb composite recipe

        :param recipe: the rgb composite recipe which the created layer gets
        as recipe
        """
        rgb_layer = self._get_empty_rgb_layer(recipe)

        if rgb_layer:
            index = self.index(rgb_layer.order, 0)
            self.didRequestSelectionOfLayer.emit(index)

    def _get_empty_rgb_layer(self, recipe: CompositeRecipe):
        # TODO(am) add check of existing layer with help of recipe
        info = {Info.KIND: Kind.RGB}

        prez = Presentation(uuid=None, kind=Kind.RGB, climits=recipe.color_limits, gamma=recipe.gammas)

        rgb_layer = LayerItem(self, info, prez, recipe=recipe)
        self.didCreateLayer.emit(rgb_layer)
        self._add_layer(rgb_layer)
        return rgb_layer

    def update_rgb_layer_color_limits(self, recipe: CompositeRecipe):
        """"""
        rgb_layer: LayerItem = self._get_layer_of_recipe(recipe.id)
        self.change_color_limits_for_layer(rgb_layer.uuid, recipe.color_limits)

    def update_rgb_layer_gamma(self, recipe: CompositeRecipe):
        rgb_layer: LayerItem = self._get_layer_of_recipe(recipe.id)
        self.change_gamma_for_layer(rgb_layer.uuid, recipe.gammas)

    @staticmethod
    def _get_datasets_uuids_of_multichannel_dataset(sched_time: datetime, input_layers: List[LayerItem]) -> List[UUID]:
        input_datasets_uuids = []
        for layer in input_layers:
            dataset_uuid = layer.timeline.get(sched_time).uuid if layer else None
            input_datasets_uuids.append(dataset_uuid)
        return input_datasets_uuids

    @staticmethod
    def _get_datasets_infos_of_multichannel_dataset(sched_time: datetime, input_layers: List[LayerItem]) -> List[dict]:
        input_datasets_infos = []
        for layer in input_layers:
            dataset_info = layer.timeline.get(sched_time).info if layer else None
            input_datasets_infos.append(dataset_info)
        return input_datasets_infos

    def _remove_datasets(self, datasets_to_remove: List[datetime], layer: LayerItem):
        for sched_time in datasets_to_remove:
            dataset: ProductDataset = layer.timeline.get(sched_time)
            self._remove_dataset(layer, sched_time, dataset.uuid)

    def _remove_dataset(self, layer: LayerItem, sched_time: datetime, dataset_uuid: UUID):
        layer.remove_dataset(sched_time)
        # TODO: Workspace has to remove Content/Product
        self.didDeleteProductDataset.emit(dataset_uuid)

    def _update_rgb_datasets(
        self, datasets_to_update: List[datetime], input_layers: List[LayerItem], rgb_layer: LayerItem
    ):
        for sched_time in datasets_to_update:
            dataset: ProductDataset = rgb_layer.timeline.get(sched_time)

            input_datasets_uuids = self._get_datasets_uuids_of_multichannel_dataset(sched_time, input_layers)
            input_datasets_infos = self._get_datasets_infos_of_multichannel_dataset(sched_time, input_layers)
            dataset_uuid = dataset.uuid

            dataset.input_datasets_uuids = input_datasets_uuids
            dataset.update_multichannel_dataset_info(input_datasets_infos)

            if not dataset.info:
                self._remove_dataset(rgb_layer, sched_time, dataset_uuid)
                continue

            self.didChangeCompositeProductDataset.emit(rgb_layer, dataset)

    def _add_rgb_datasets(self, datasets_to_added: List[datetime], input_layers: List[LayerItem], rgb_layer: LayerItem):
        for sched_time in datasets_to_added:
            input_datasets_uuids = self._get_datasets_uuids_of_multichannel_dataset(sched_time, input_layers)
            input_datasets_infos = self._get_datasets_infos_of_multichannel_dataset(sched_time, input_layers)

            dataset = rgb_layer.add_multichannel_dataset(None, sched_time, input_datasets_uuids, input_datasets_infos)

            self.didAddCompositeDataset.emit(rgb_layer, dataset)

    @staticmethod
    def _get_diff_of_timelines(common_timeline: List[datetime], rgb_layer: LayerItem):
        datasets_to_added = []
        datasets_to_remove = list(rgb_layer.timeline.keys())
        datasets_to_update = []
        for timestep in common_timeline:
            if timestep in rgb_layer.timeline.keys():
                datasets_to_update.append(timestep)
                datasets_to_remove.remove(timestep)
            elif timestep not in rgb_layer.timeline.keys():
                datasets_to_added.append(timestep)
        return datasets_to_added, datasets_to_update, datasets_to_remove

    @staticmethod
    def _get_common_timeline_of_input_layers(timelines_to_compare: List[dict]):
        if len(timelines_to_compare) == 0:
            return []

        intersection = timelines_to_compare[-1].keys()
        for idx in range(len(timelines_to_compare) - 1):
            curr_timeline = timelines_to_compare[idx]
            intersection = intersection & curr_timeline.keys()

        return intersection

    @staticmethod
    def _get_timeline_of_layers(input_layers: List[LayerItem]):
        timelines_of_input_layers = []
        for layer in input_layers:
            if layer:
                timelines_of_input_layers.append(layer.timeline)
        return timelines_of_input_layers

    def update_recipe_layer_timeline(self, recipe: Recipe):
        recipe_layer: LayerItem = self._get_layer_of_recipe(recipe.id)

        if isinstance(recipe, CompositeRecipe):
            self.update_rgb_layer_gamma(recipe)
            self.update_rgb_layer_color_limits(recipe)

        input_layers = self.get_layers_by_uuids(recipe_layer.recipe.input_layer_ids)

        timelines_to_compare = self._get_timeline_of_layers(input_layers)
        common_timeline = self._get_common_timeline_of_input_layers(timelines_to_compare)

        sched_times_to_add, existing_sched_times, sched_times_to_remove = self._get_diff_of_timelines(
            common_timeline, recipe_layer
        )

        self._remove_datasets(sched_times_to_remove, recipe_layer)

        sched_times_to_update = self._check_recipe_layer_sched_times_to_update(
            existing_sched_times, input_layers, recipe_layer
        )

        if isinstance(recipe, CompositeRecipe):
            self._update_rgb_datasets(sched_times_to_update, input_layers, recipe_layer)
            self._add_rgb_datasets(sched_times_to_add, input_layers, recipe_layer)

        elif isinstance(recipe, AlgebraicRecipe):
            self._update_algebraic_datasets(sched_times_to_update, input_layers, recipe_layer)
            self._add_algebraic_datasets(sched_times_to_add, input_layers, recipe_layer)
            recipe_layer.recipe.modified = False

            self._trigger_composite_layer_update(recipe_layer)

        self.didUpdateLayers.emit()

    def _check_recipe_layer_sched_times_to_update(self, existing_sched_times, input_layers, recipe_layer):
        if isinstance(recipe_layer.recipe, AlgebraicRecipe):
            if recipe_layer.recipe.modified:
                return existing_sched_times

        sched_times_to_update = []
        for sched_time in existing_sched_times:
            dataset: ProductDataset = recipe_layer.timeline.get(sched_time)
            input_datasets_uuids = self._get_datasets_uuids_of_multichannel_dataset(sched_time, input_layers)
            if dataset.input_datasets_uuids != input_datasets_uuids:
                sched_times_to_update.append(sched_time)
        return sched_times_to_update

    def _get_layer_of_recipe(self, recipe_id: UUID):
        """Get layer which has the given recipe as a attribute

        :param recipe_id: recipe which is used to search the wanted layers
        :return: the searched layers
        """
        return [layer for layer in self.layers if layer.recipe and layer.recipe.id == recipe_id][-1]

    def get_layers_by_uuids(self, layer_uuids: List[UUID]):
        """Get layers which have the given identifiers as a attribute.

        :param layer_uuids: identifiers which are used to search the
        wanted layers
        :return: the searched layers
        """
        layers = []
        for uuid in layer_uuids:
            layers.append(self.get_layer_by_uuid(uuid))
        return layers

    def _trigger_composite_layer_update(self, changed_layer):
        for layer in self.layers:
            if layer.recipe:
                if changed_layer.uuid in layer.recipe.input_layer_ids:
                    self._update_composite_layer_timeline(layer)

    def _update_composite_layer_timeline(self, layer):
        if isinstance(layer.recipe, CompositeRecipe):
            self.update_recipe_layer_timeline(layer.recipe)

    @staticmethod
    def create_reasonable_rgb_composite_default():
        """Creates a reasonable default layer list for rgb composites
        :return: the reasonable default layer list
        """
        return [None, None, None]

    def start_rgb_composite_creation(self, layers=None):
        """starts creation of rgb composite recipe.

        :param layers: The layers which will be used to create a rgb composite.
            - Layer at the index 0 will be used for the red component
            of the rgb.
            - Layer at the index 1 will be used for the green component
            of the rgb.
            - Layer at the index 2 will be used for the blue component
            of the rgb.
        """

        if not layers or len(layers) == 0:
            layers = self.create_reasonable_rgb_composite_default()

        # TODO: case when layers list has less then 3 elements
        # layers = []

        self.didRequestCompositeRecipeCreation.emit(layers)

    def update_recipe_layer_name(self, recipe: Recipe):
        recipe_layer: LayerItem = self._get_layer_of_recipe(recipe.id)
        recipe_layer.update_invariable_display_data()
        self.didChangeRecipeLayerNames.emit()

        index = self.index(recipe_layer.order, LMC.NAME)
        self.dataChanged.emit(index, index)

    @staticmethod
    def create_reasonable_algebraic_composite_default():
        """Creates a reasonable default layer list for algebraic composites
        :return: the reasonable default layer list
        """
        return [None, None, None]

    def start_algebraic_composite_creation(self, layers=None):
        """starts creation of an algebraic composite recipe.

        :param layers: The layers which will be used to create a rgb composite.
            - Layer at the index 0 will be used for the x component
            of the algebraic.
            - Layer at the index 1 will be used for the y component
            of the algebraic.
            - Layer at the index 2 will be used for the z component
            of the algebraic.
        """

        if not layers or len(layers) == 0:
            layers = self.create_reasonable_algebraic_composite_default()

        self.didRequestAlgebraicRecipeCreation.emit(layers)

    def create_algebraic_composite_layer(self, recipe: AlgebraicRecipe):
        """Creates a layer which has an algebraic composite recipe

        :param recipe: the algebraic composite recipe which the created layer
         gets as recipe
        """
        algebraic_layer = self._get_empty_algebraic_layer(recipe)

        if algebraic_layer:
            index = self.index(algebraic_layer.order, 0)
            self.didRequestSelectionOfLayer.emit(index)

    def _get_empty_algebraic_layer(self, recipe: AlgebraicRecipe):
        info = {
            Info.KIND: Kind.COMPOSITE,
        }

        prez = Presentation(uuid=None, kind=Kind.COMPOSITE, colormap="grays", climits=(-100, 100))

        algebraic_layer = LayerItem(self, info, prez, recipe=recipe)

        self.didCreateLayer.emit(algebraic_layer)
        self._add_layer(algebraic_layer)
        return algebraic_layer

    def _add_algebraic_datasets(
        self, sched_times: List[datetime], input_layers: List[LayerItem], algebraic_layer: LayerItem
    ):
        assert isinstance(algebraic_layer.recipe, AlgebraicRecipe)

        dataset_info = None
        for sched_time in sched_times:
            input_datasets_uuids = self._get_datasets_uuids_of_multichannel_dataset(sched_time, input_layers)

            info = {
                Info.SHORT_NAME: algebraic_layer.recipe.name,
            }
            # Skip if input dataset uuid is None
            # and assign placeholders to the others
            assignment = dict([p for p in zip("xyz", input_datasets_uuids) if p[1]])

            operations = algebraic_layer.recipe.operation_formula

            uuid, info, data = self._workspace.create_algebraic_composite(operations, assignment, info)

            dataset = algebraic_layer.add_algebraic_dataset(None, info, sched_time, input_datasets_uuids)

            self.didAddImageDataset.emit(algebraic_layer, dataset)
            dataset_info = info
        if dataset_info:
            algebraic_layer.info = LayerItem.extract_layer_info(dataset_info)

    def _update_algebraic_datasets(
        self, sched_times: List[datetime], input_layers: List[LayerItem], algebraic_layer: LayerItem
    ):
        self._remove_datasets(sched_times, algebraic_layer)
        self._add_algebraic_datasets(sched_times, input_layers, algebraic_layer)

    def toggle_layers_visibility(self, indexes: List[QModelIndex]):
        for index in indexes:
            if index.column() != LMC.VISIBILITY:
                continue
            layer = self.layers[index.row()]
            layer_visibility = LayerVisibility(not layer.visible, layer.opacity)
            self.setData(index, layer_visibility)

    def get_dataset_by_uuid(self, dataset_uuid: UUID) -> Optional[ProductDataset]:
        """
        Find a dataset given by its uuid in the layer model and return it, None
        if it is not in the model.

        :param dataset_uuid:
        :return: dataset if found, None else
        """
        for layer in self.layers:
            dataset = layer.get_dataset_by_uuid(dataset_uuid)
            if dataset:
                return dataset
        return None

    def remove_datasets_from_all_layers(self, dataset_uuids):
        did_remove_any_dataset = False
        for dataset_uuid in dataset_uuids:
            dataset = self.get_dataset_by_uuid(dataset_uuid)
            LOG.debug(f"Dataset for uuid {dataset_uuid}: {dataset}")
            if dataset:
                layer = self.get_layer_by_uuid(dataset.layer_uuid)
                self._remove_dataset(layer, dataset.info[Info.SCHED_TIME], dataset.info[Info.UUID])
                LOG.debug(f"Removing {dataset}")
                self._document.remove_layer_prez(dataset_uuid)
                self._document.purge_layer_prez([dataset_uuid])
                did_remove_any_dataset = True

        if did_remove_any_dataset:
            self.didUpdateLayers.emit()


class ProductFamilyKeyMappingPolicy:
    def __init__(self, model: LayerModel):
        self.model = model

    def get_existing_layer_for_dataset(self, info: frozendict):
        """
        Returns layer within an instance of LayerModel according to a match
        between the `grouping_key` calculated from the given dataset metadata
        information and the `grouping_key` s within LayerModel's `layers`
        collection.

        :param info: Dataset metadata information

        :return: tuple with LayerItem with its `grouping_key` matching that
            of the passed dataset metadata information, if there is already one
            in the LayerModel, None otherwise. Second element of the tuple is
            the grouping key generated by the policy.
            You must use that key when creating a new layer for the dataset for
            the given info to make the policy work.
        """
        product_family_key = self.get_grouping_key(info)
        LOG.debug(f"Product Family Key:\n {product_family_key}")

        existing_product_family_keys = [layer.grouping_key for layer in self.model.layers]

        if product_family_key in existing_product_family_keys:
            layer_idx = existing_product_family_keys.index(product_family_key)
            return self.model.layers[layer_idx], product_family_key

        return None, product_family_key

    @staticmethod
    def get_grouping_key(info):
        # This is, where layer grouping policies will differ:
        # This implementation returns the (legacy SIFT) product_family_key
        return info.get(Info.PLATFORM), info.get(Info.INSTRUMENT), info.get(Info.DATASET_NAME)
