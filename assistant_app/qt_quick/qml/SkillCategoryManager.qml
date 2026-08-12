import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: root
    required property var assistantBackend
    property color pageBackground: "#F5F5F7"
    property color cardColor: "#FFFFFF"
    property color textColor: "#1D1D1F"
    property color secondaryText: "#6E6E73"
    property color borderColor: "#E3E3E8"
    property color accent: "#1687FF"
    property string pendingDelete: ""

    function matchesSkill(item, query) {
        const needle = query.trim().toLowerCase()
        if (needle === "") return true
        return (item.name + " " + item.description + " " + item.category).toLowerCase().indexOf(needle) >= 0
    }

    anchors.centerIn: Overlay.overlay
    width: Math.min(620, Overlay.overlay.width - 80)
    height: Math.min(650, Overlay.overlay.height - 60)
    modal: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: Rectangle { color: root.pageBackground; radius: 30; border.color: root.borderColor }

    component SoftButton: Button {
        id: control
        implicitHeight: 34
        leftPadding: 12
        rightPadding: 12
        font.pixelSize: 11
        contentItem: Text { text: control.text; color: control.enabled ? root.textColor : "#A0A0A5"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: control.font }
        background: Rectangle { radius: height / 2; color: control.hovered ? "#E2E2E7" : "#F0F0F3" }
    }

    component BlueButton: Button {
        id: control
        implicitHeight: 38
        leftPadding: 15
        rightPadding: 15
        font.pixelSize: 12
        font.bold: true
        contentItem: Text { text: control.text; color: "white"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: control.font }
        background: Rectangle { radius: height / 2; color: control.hovered ? "#2D95FF" : root.accent }
    }

    component DangerButton: Button {
        id: control
        implicitHeight: 34
        leftPadding: 12
        rightPadding: 12
        font.pixelSize: 11
        font.bold: true
        contentItem: Text { text: control.text; color: "#D70015"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: control.font }
        background: Rectangle { radius: height / 2; color: control.hovered ? "#FFD7D7" : "#FFE8E8" }
    }

    Popup {
        id: deleteConfirmation
        anchors.centerIn: Overlay.overlay
        width: Math.min(440, Overlay.overlay.width - 80)
        modal: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: Rectangle { color: root.cardColor; radius: 24; border.color: root.borderColor }
        contentItem: ColumnLayout {
            spacing: 14
            Label { text: "删除自定义分类？"; font.pixelSize: 19; font.bold: true; color: root.textColor }
            Label { Layout.fillWidth: true; text: "“" + root.pendingDelete + "”中的技能会恢复到各自的内置分类。"; wrapMode: Text.Wrap; color: root.secondaryText }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                SoftButton { text: "取消"; onClicked: deleteConfirmation.close() }
                DangerButton { text: "删除"; onClicked: { root.assistantBackend.removeSkillCategory(root.pendingDelete); deleteConfirmation.close() } }
            }
        }
    }

    contentItem: ColumnLayout {
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Label { text: "技能分类"; font.pixelSize: 20; font.bold: true; color: root.textColor }
            Item { Layout.fillWidth: true }
            SoftButton { text: "关闭"; onClicked: root.close() }
        }
        Label { Layout.fillWidth: true; text: "添加自己的分类，再为技能选择归属。内置分类保持可用。"; wrapMode: Text.Wrap; color: root.secondaryText; font.pixelSize: 11 }
        RowLayout {
            Layout.fillWidth: true
            TextField {
                id: newCategoryName
                Layout.fillWidth: true
                implicitHeight: 38
                maximumLength: 20
                placeholderText: "新分类名称"
                background: Rectangle { radius: 19; color: root.cardColor; border.color: root.borderColor }
                function addCategory() {
                    if (root.assistantBackend.addSkillCategory(text)) text = ""
                }
                Keys.onReturnPressed: addCategory()
            }
            BlueButton { text: "添加"; enabled: newCategoryName.text.trim() !== ""; onClicked: newCategoryName.addCategory() }
        }
        Label { Layout.fillWidth: true; visible: root.assistantBackend.skillCategoryStatus !== ""; text: root.assistantBackend.skillCategoryStatus; color: root.secondaryText; font.pixelSize: 11; wrapMode: Text.Wrap }
        Label { text: "自定义分类"; font.bold: true; color: root.textColor }
        ListView {
            id: customCategoryList
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(44, Math.min(contentHeight, 150))
            clip: true; spacing: 6
            model: root.assistantBackend.customSkillCategories
            delegate: Rectangle {
                required property string modelData
                required property int index
                width: customCategoryList.width; height: 44; radius: 18; color: root.cardColor; border.color: root.borderColor
                RowLayout {
                    anchors.fill: parent; anchors.margins: 5
                    TextField { id: categoryNameEditor; Layout.fillWidth: true; maximumLength: 20; text: modelData; background: Rectangle { radius: 15; color: "#F0F0F3" } Keys.onReturnPressed: root.assistantBackend.renameSkillCategory(modelData, text) }
                    SoftButton { implicitWidth: 32; text: "↑"; enabled: index > 0; onClicked: root.assistantBackend.moveSkillCategory(modelData, -1) }
                    SoftButton { implicitWidth: 32; text: "↓"; enabled: index < customCategoryList.count - 1; onClicked: root.assistantBackend.moveSkillCategory(modelData, 1) }
                    SoftButton { text: "重命名"; enabled: categoryNameEditor.text.trim() !== "" && categoryNameEditor.text.trim() !== modelData; onClicked: root.assistantBackend.renameSkillCategory(modelData, categoryNameEditor.text) }
                    DangerButton { text: "删除"; onClicked: { root.pendingDelete = modelData; deleteConfirmation.open() } }
                }
            }
            Label { anchors.centerIn: parent; visible: customCategoryList.count === 0; text: "还没有自定义分类"; color: root.secondaryText; font.pixelSize: 11 }
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: root.borderColor }
        Label { text: "技能归类"; font.bold: true; color: root.textColor }
        RoundedSearchField { id: categorySkillSearch; Layout.fillWidth: true; placeholderText: "搜索待归类技能"; fillColor: root.cardColor; borderColor: root.borderColor }
        Label { Layout.fillWidth: true; visible: categorySkillSearch.text.trim() !== ""; text: categorySkillList.count + " 个匹配结果"; color: root.secondaryText; font.pixelSize: 10; horizontalAlignment: Text.AlignRight }
        ListView {
            id: categorySkillList
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 6
            model: root.assistantBackend.skills.filter(item => root.matchesSkill(item, categorySkillSearch.text))
            delegate: Rectangle {
                id: categorySkillRow
                required property var modelData
                width: categorySkillList.width; height: 48; radius: 18; color: root.cardColor; border.color: root.borderColor
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 14; anchors.rightMargin: 8
                    Label { Layout.fillWidth: true; text: categorySkillRow.modelData.name; font.bold: true; color: root.textColor }
                    ComboBox {
                        implicitWidth: 190; implicitHeight: 34
                        model: root.assistantBackend.skillCategories
                        currentIndex: Math.max(0, root.assistantBackend.skillCategories.indexOf(categorySkillRow.modelData.category))
                        background: Rectangle { radius: 17; color: "#E9E9ED" }
                        onActivated: root.assistantBackend.setSkillCategory(categorySkillRow.modelData.id, currentText)
                    }
                }
            }
            Label { anchors.centerIn: parent; visible: categorySkillList.count === 0; text: "没有匹配的技能"; color: root.secondaryText; font.pixelSize: 11 }
        }
    }
}
