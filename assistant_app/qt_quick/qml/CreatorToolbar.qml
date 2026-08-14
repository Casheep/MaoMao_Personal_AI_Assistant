import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var assistantBackend
    property bool expanded: false
    property string generatorKind: "workflow"
    property color cardColor: "#FFFFFF"
    property color textColor: "#1D1D1F"
    property color secondaryText: "#6E6E73"
    property color borderColor: "#E3E3E8"
    property color accent: "#1687FF"

    component SoftButton: Button {
        id: control
        implicitHeight: 34
        leftPadding: 12; rightPadding: 12
        font.pixelSize: 11
        contentItem: Text { text: control.text; color: root.textColor; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: control.font }
        background: Rectangle { radius: height / 2; color: control.hovered ? "#E2E2E7" : "#F0F0F3" }
    }
    component BlueButton: Button {
        id: control
        implicitHeight: 36
        leftPadding: 14; rightPadding: 14
        font.pixelSize: 11; font.bold: true
        contentItem: Text { text: control.text; color: "white"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: control.font }
        background: Rectangle { radius: height / 2; color: control.enabled ? (control.hovered ? "#2D95FF" : root.accent) : "#A8CFF5" }
    }
    component DangerButton: Button {
        id: control
        implicitHeight: 30
        leftPadding: 10; rightPadding: 10
        font.pixelSize: 10
        contentItem: Text { text: control.text; color: "#D70015"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: control.font }
        background: Rectangle { radius: height / 2; color: control.hovered ? "#FFD7D7" : "#FFE8E8" }
    }

    Rectangle {
        id: drawer
        objectName: "creatorToolbarDrawer"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.topMargin: 17
        height: root.expanded ? root.height - 17 : 0
        radius: 28
        color: root.cardColor
        border.color: root.borderColor
        border.width: 1
        clip: true
        visible: height > 0

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 14
            anchors.rightMargin: 14
            anchors.topMargin: 34
            anchors.bottomMargin: 14
            spacing: 12

            Rectangle {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                radius: 20; color: "#F8F8FA"; border.color: root.borderColor
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 7
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: "技能生成器"; font.pixelSize: 16; font.bold: true; color: root.textColor }
                        Item { Layout.fillWidth: true }
                        SoftButton { text: "组合技能"; font.bold: root.generatorKind === "workflow"; onClicked: root.generatorKind = "workflow" }
                        SoftButton { text: "代码技能"; font.bold: root.generatorKind === "code"; onClicked: root.generatorKind = "code" }
                    }
                    Label {
                        Layout.fillWidth: true
                        text: root.generatorKind === "workflow" ? "编排猫猫已有工具，不生成执行代码。" : "Kimi K3 harness 生成并修正 Python 技能；保存前检查代码和权限，执行前再次确认。"
                        color: root.secondaryText; font.pixelSize: 10; wrapMode: Text.Wrap
                    }
                    TextArea {
                        id: skillRequest
                        Layout.fillWidth: true; Layout.fillHeight: true
                        placeholderText: root.generatorKind === "workflow" ? "描述想让猫猫组合完成的事情、触发方式和结果…" : "描述新代码技能要做什么、输入和预期输出…"
                        wrapMode: TextEdit.Wrap; color: root.textColor
                        background: Rectangle { color: root.cardColor; radius: 16; border.color: root.borderColor }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: root.assistantBackend.skillGeneratorStatus; color: root.secondaryText; font.pixelSize: 10; elide: Text.ElideRight }
                        BlueButton { text: root.assistantBackend.skillGeneratorBusy ? (root.generatorKind === "code" ? "Harness 生成中" : "Kimi K3 生成中") : "生成草案"; enabled: !root.assistantBackend.skillGeneratorBusy && skillRequest.text.trim().length >= 6; onClicked: root.assistantBackend.generateSkill(root.generatorKind, skillRequest.text) }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.preferredWidth: 1
                radius: 20; color: "#F8F8FA"; border.color: root.borderColor
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 6
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: root.assistantBackend.generatedSkillDraft.name ? "草案 · " + root.assistantBackend.generatedSkillDraft.name : "生成结果"; font.pixelSize: 16; font.bold: true; color: root.textColor }
                        Item { Layout.fillWidth: true }
                        SoftButton { visible: !!root.assistantBackend.generatedSkillDraft.name; text: "放弃"; onClicked: root.assistantBackend.discardGeneratedSkillDraft() }
                        BlueButton { visible: !!root.assistantBackend.generatedSkillDraft.name; text: "确认保存"; onClicked: root.assistantBackend.saveGeneratedSkillDraft() }
                    }
                    ScrollView {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        visible: !!root.assistantBackend.generatedSkillDraft.name
                        TextArea {
                            readOnly: true; wrapMode: TextEdit.Wrap; color: root.textColor
                            text: root.assistantBackend.generatedSkillDraft.name
                                  ? root.assistantBackend.generatedSkillDraft.description
                                    + "\n\n触发：" + root.assistantBackend.generatedSkillDraft.triggers.join("、")
                                    + "\n分类：" + root.assistantBackend.generatedSkillDraft.category
                                    + (root.assistantBackend.generatedSkillDraft.kind === "workflow" ? "\n使用工具：" + root.assistantBackend.generatedSkillDraft.toolSummary + "\n\n执行步骤：\n" + root.assistantBackend.generatedSkillDraft.instruction : "\n生成方式：" + root.assistantBackend.generatedSkillDraft.generator + "\n代码权限：" + root.assistantBackend.generatedSkillDraft.permissionSummary + "\n\nPython 代码：\n" + root.assistantBackend.generatedSkillDraft.code)
                                  : ""
                            background: Rectangle { color: root.cardColor; radius: 14; border.color: root.borderColor }
                        }
                    }
                    Label { Layout.fillWidth: true; Layout.fillHeight: true; visible: !root.assistantBackend.generatedSkillDraft.name && root.assistantBackend.generatedSkills.length === 0; text: "生成的草案会先显示在这里。\n确认后才会保存到本机并出现在技能栏。"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; wrapMode: Text.Wrap; color: root.secondaryText }
                    ListView {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        visible: !root.assistantBackend.generatedSkillDraft.name && root.assistantBackend.generatedSkills.length > 0
                        clip: true; spacing: 6; model: root.assistantBackend.generatedSkills
                        delegate: Rectangle {
                            required property var modelData
                            width: ListView.view.width; height: 44; radius: 16; color: root.cardColor; border.color: root.borderColor
                            RowLayout {
                                anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 6
                                Label { Layout.fillWidth: true; text: modelData.name + " · " + (modelData.kind === "code" ? "代码技能" : "组合技能"); color: root.textColor; elide: Text.ElideRight }
                                DangerButton { text: "删除"; onClicked: root.assistantBackend.deleteGeneratedSkill(modelData.id) }
                            }
                        }
                    }
                }
            }
        }
    }

    Button {
        id: toolbarTab
        objectName: "creatorToolbarToggle"
        anchors.top: parent.top
        anchors.horizontalCenter: parent.horizontalCenter
        width: 118
        height: 36
        z: 3
        text: "工具栏"
        font.pixelSize: 12
        font.bold: true
        contentItem: Text { text: toolbarTab.text; color: "#6B4FD3"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: toolbarTab.font }
        background: Rectangle {
            radius: 18
            color: toolbarTab.hovered ? "#E2DAFF" : "#EEE9FF"
            border.color: "#D8CFF5"
        }
        onClicked: root.expanded = !root.expanded
    }
}
