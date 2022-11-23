# -*- coding: utf-8 -*-

# Form implementation generated from reading ui file 'layer_details_widget.ui'
#
# Created by: PyQt5 UI code generator 5.15.7
#
# WARNING: Any manual changes made to this file will be lost when pyuic5 is
# run again.  Do not edit this file unless you know what you are doing.


from PyQt5 import QtCore, QtGui, QtWidgets


class Ui_LayerDetailsPane(object):
    def setupUi(self, LayerDetailsPane):
        LayerDetailsPane.setObjectName("LayerDetailsPane")
        LayerDetailsPane.resize(312, 228)
        sizePolicy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Minimum)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(LayerDetailsPane.sizePolicy().hasHeightForWidth())
        LayerDetailsPane.setSizePolicy(sizePolicy)
        self.verticalLayout = QtWidgets.QVBoxLayout(LayerDetailsPane)
        self.verticalLayout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
        self.verticalLayout.setContentsMargins(6, 6, 6, 6)
        self.verticalLayout.setSpacing(6)
        self.verticalLayout.setObjectName("verticalLayout")
        self.formLayout = QtWidgets.QFormLayout()
        self.formLayout.setSizeConstraint(QtWidgets.QLayout.SetFixedSize)
        self.formLayout.setSpacing(6)
        self.formLayout.setObjectName("formLayout")
        self.layerNameLabel = QtWidgets.QLabel(LayerDetailsPane)
        self.layerNameLabel.setObjectName("layerNameLabel")
        self.formLayout.setWidget(0, QtWidgets.QFormLayout.LabelRole, self.layerNameLabel)
        self.layerNameValue = QtWidgets.QLabel(LayerDetailsPane)
        self.layerNameValue.setObjectName("layerNameValue")
        self.formLayout.setWidget(0, QtWidgets.QFormLayout.FieldRole, self.layerNameValue)
        self.layerVisibleSchedTimeLabel = QtWidgets.QLabel(LayerDetailsPane)
        self.layerVisibleSchedTimeLabel.setObjectName("layerVisibleSchedTimeLabel")
        self.formLayout.setWidget(1, QtWidgets.QFormLayout.LabelRole, self.layerVisibleSchedTimeLabel)
        self.layerVisibleSchedTimeValue = QtWidgets.QLabel(LayerDetailsPane)
        self.layerVisibleSchedTimeValue.setObjectName("layerVisibleSchedTimeValue")
        self.formLayout.setWidget(1, QtWidgets.QFormLayout.FieldRole, self.layerVisibleSchedTimeValue)
        self.layerInstrumentLabel = QtWidgets.QLabel(LayerDetailsPane)
        self.layerInstrumentLabel.setObjectName("layerInstrumentLabel")
        self.formLayout.setWidget(2, QtWidgets.QFormLayout.LabelRole, self.layerInstrumentLabel)
        self.layerInstrumentValue = QtWidgets.QLabel(LayerDetailsPane)
        self.layerInstrumentValue.setObjectName("layerInstrumentValue")
        self.formLayout.setWidget(2, QtWidgets.QFormLayout.FieldRole, self.layerInstrumentValue)
        self.layerWavelengthLabel = QtWidgets.QLabel(LayerDetailsPane)
        self.layerWavelengthLabel.setObjectName("layerWavelengthLabel")
        self.formLayout.setWidget(3, QtWidgets.QFormLayout.LabelRole, self.layerWavelengthLabel)
        self.layerWavelengthValue = QtWidgets.QLabel(LayerDetailsPane)
        self.layerWavelengthValue.setObjectName("layerWavelengthValue")
        self.formLayout.setWidget(3, QtWidgets.QFormLayout.FieldRole, self.layerWavelengthValue)
        self.layerColormapLabel = QtWidgets.QLabel(LayerDetailsPane)
        self.layerColormapLabel.setObjectName("layerColormapLabel")
        self.formLayout.setWidget(4, QtWidgets.QFormLayout.LabelRole, self.layerColormapLabel)
        self.layerColormapValue = QtWidgets.QLabel(LayerDetailsPane)
        self.layerColormapValue.setObjectName("layerColormapValue")
        self.formLayout.setWidget(4, QtWidgets.QFormLayout.FieldRole, self.layerColormapValue)
        self.layerColorLimitsLabel = QtWidgets.QLabel(LayerDetailsPane)
        self.layerColorLimitsLabel.setObjectName("layerColorLimitsLabel")
        self.formLayout.setWidget(5, QtWidgets.QFormLayout.LabelRole, self.layerColorLimitsLabel)
        self.layerColorLimitsValue = QtWidgets.QLabel(LayerDetailsPane)
        self.layerColorLimitsValue.setObjectName("layerColorLimitsValue")
        self.formLayout.setWidget(5, QtWidgets.QFormLayout.FieldRole, self.layerColorLimitsValue)
        self.verticalLayout.addLayout(self.formLayout)
        self.layerColormapVisual = QNoScrollWebView(LayerDetailsPane)
        self.layerColormapVisual.setMinimumSize(QtCore.QSize(300, 30))
        self.layerColormapVisual.setMaximumSize(QtCore.QSize(300, 30))
        self.layerColormapVisual.setObjectName("layerColormapVisual")
        self.verticalLayout.addWidget(self.layerColormapVisual)
        spacerItem = QtWidgets.QSpacerItem(20, 40, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding)
        self.verticalLayout.addItem(spacerItem)

        self.retranslateUi(LayerDetailsPane)
        QtCore.QMetaObject.connectSlotsByName(LayerDetailsPane)

    def retranslateUi(self, LayerDetailsPane):
        _translate = QtCore.QCoreApplication.translate
        LayerDetailsPane.setWindowTitle(_translate("LayerDetailsPane", "Form"))
        self.layerNameLabel.setText(_translate("LayerDetailsPane", "Name:"))
        self.layerNameValue.setText(_translate("LayerDetailsPane", "layer_descriptor"))
        self.layerVisibleSchedTimeLabel.setText(_translate("LayerDetailsPane", "Time:"))
        self.layerVisibleSchedTimeValue.setText(_translate("LayerDetailsPane", "2019-10-21 12:00:10"))
        self.layerInstrumentLabel.setText(_translate("LayerDetailsPane", "Instrument:"))
        self.layerInstrumentValue.setText(_translate("LayerDetailsPane", "SEVIRI"))
        self.layerWavelengthLabel.setText(_translate("LayerDetailsPane", "Wavelength:"))
        self.layerWavelengthValue.setText(_translate("LayerDetailsPane", "3.92 "))
        self.layerColormapLabel.setText(_translate("LayerDetailsPane", "Colormap:"))
        self.layerColormapValue.setText(_translate("LayerDetailsPane", "Rainbow (IR Default)"))
        self.layerColorLimitsLabel.setText(_translate("LayerDetailsPane", "Color Limits:"))
        self.layerColorLimitsValue.setText(_translate("LayerDetailsPane", "-109.00 ~ 55.00°C"))


from uwsift.ui.custom_widgets import QNoScrollWebView

if __name__ == "__main__":
    import sys

    app = QtWidgets.QApplication(sys.argv)
    LayerDetailsPane = QtWidgets.QWidget()
    ui = Ui_LayerDetailsPane()
    ui.setupUi(LayerDetailsPane)
    LayerDetailsPane.show()
    sys.exit(app.exec_())
