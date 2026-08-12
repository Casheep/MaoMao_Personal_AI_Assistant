import QtQuick
import QtQuick.Controls

TextField {
    id: control
    property color fillColor: "#F0F0F3"
    property color borderColor: "transparent"

    implicitHeight: 34
    leftPadding: 14
    rightPadding: 38
    background: Rectangle {
        radius: height / 2
        color: control.fillColor
        border.color: control.borderColor
    }
    Button {
        anchors.right: parent.right
        anchors.rightMargin: 5
        anchors.verticalCenter: parent.verticalCenter
        width: 26
        height: 26
        visible: control.text !== ""
        text: "×"
        font.pixelSize: 15
        contentItem: Text {
            text: parent.text
            color: "#6E6E73"
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: parent.font
        }
        background: Rectangle {
            radius: 13
            color: parent.hovered ? "#DADAE0" : "transparent"
        }
        onClicked: {
            control.clear()
            control.forceActiveFocus()
        }
    }
}
