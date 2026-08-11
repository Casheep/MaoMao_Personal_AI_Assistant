import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import QtQuick.Window

ApplicationWindow {
    id: window
    width: Math.max(minimumWidth, Math.min(1540, Screen.width - 80))
    height: Math.max(minimumHeight, Math.min(800, Screen.height - 80))
    minimumWidth: 1180
    minimumHeight: 640
    visible: true
    title: "MaoMao"
    color: "#F5F5F7"

    property bool forceClose: false
    property bool skillOpen: assistant.skillSidebarExpanded
    property bool favoriteOpen: assistant.favoriteSidebarExpanded
    property string skillMode: "learned"
    property string skillCategory: "全部"
    property color pageBackground: "#F5F5F7"
    property color cardColor: "#FFFFFF"
    property color textColor: "#1D1D1F"
    property color secondaryText: "#6E6E73"
    property color borderColor: "#E3E3E8"
    property color accent: "#1687FF"

    onClosing: close => {
        if (!forceClose) {
            close.accepted = false
            window.hide()
        }
    }

    component Card: Rectangle {
        radius: 34
        color: window.cardColor
        border.color: window.borderColor
        border.width: 1
    }

    component BlueButton: Button {
        id: control
        implicitHeight: 42
        leftPadding: 17
        rightPadding: 17
        font.pixelSize: 13
        font.bold: true
        contentItem: Text {
            text: control.text
            color: "white"
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
        background: Rectangle {
            radius: height / 2
            color: control.down ? "#0070E8" : (control.hovered ? "#2D95FF" : window.accent)
        }
    }

    component SoftButton: Button {
        id: control
        implicitHeight: 34
        leftPadding: 13
        rightPadding: 13
        font.pixelSize: 11
        contentItem: Text {
            text: control.text
            color: control.enabled ? window.textColor : "#A0A0A5"
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
        background: Rectangle {
            radius: height / 2
            color: control.down ? "#DADAE0" : (control.hovered ? "#E2E2E7" : "#F0F0F3")
        }
    }

    component AccentSoftButton: Button {
        id: control
        implicitHeight: 34
        leftPadding: 13
        rightPadding: 13
        font.pixelSize: 11
        font.bold: true
        contentItem: Text {
            text: control.text
            color: window.accent
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
        background: Rectangle {
            radius: height / 2
            color: control.hovered ? "#D8E9FF" : "#E8F2FF"
        }
    }

    component DangerButton: Button {
        id: control
        implicitHeight: 34
        leftPadding: 13
        rightPadding: 13
        font.pixelSize: 11
        font.bold: true
        contentItem: Text {
            text: control.text
            color: "#D70015"
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
        background: Rectangle { radius: height / 2; color: control.hovered ? "#FFD7D7" : "#FFE8E8" }
    }

    component BlueSwitch: Switch {
        id: control
        spacing: 8
        implicitWidth: indicator.implicitWidth + (text === "" ? 0 : contentItem.implicitWidth + spacing)
        implicitHeight: 28
        font.pixelSize: 12
        indicator: Rectangle {
            implicitWidth: 44
            implicitHeight: 24
            x: 0
            y: (control.height - height) / 2
            radius: height / 2
            color: control.checked ? window.accent : "#3A3A3C"
            Rectangle {
                width: 20
                height: 20
                y: 2
                x: control.checked ? parent.width - width - 2 : 2
                radius: width / 2
                color: "#FFFFFF"
                border.color: "#D1D1D6"
                Behavior on x { NumberAnimation { duration: 100 } }
            }
        }
        contentItem: Text {
            text: control.text
            leftPadding: control.indicator.width + control.spacing
            color: window.textColor
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
    }

    component StarButton: Button {
        id: control
        implicitWidth: 28
        implicitHeight: 26
        font.pixelSize: 15
        contentItem: Text {
            text: control.text
            color: "#F5A623"
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
        background: Rectangle {
            radius: height / 2
            color: control.hovered ? "#FFF3D9" : "transparent"
        }
    }

    component CompactCombo: ComboBox {
        id: control
        implicitHeight: 34
        font.pixelSize: 11
        leftPadding: 13
        rightPadding: 28
        background: Rectangle { radius: height / 2; color: "#E9E9ED" }
    }

    component SettingTitle: Label {
        font.pixelSize: 20
        font.bold: true
        color: window.textColor
    }

    function comboIndex(items, value) {
        for (let i = 0; i < items.length; ++i)
            if (items[i].value === value) return i
        return 0
    }

    function managerTitle() {
        if (manager.pageIndex === 1) return "权限与常用动作"
        if (manager.pageIndex === 2) return "定时任务"
        if (manager.pageIndex === 3) return "关于猫猫"
        if (manager.settingsKind === "wake-word") return "唤醒词"
        if (manager.settingsKind === "continuous-conversation") return "连续对话"
        if (manager.settingsKind === "desk-lamp") return "小米智能家居"
        if (manager.settingsKind === "starrail-dailies") return "星铁日常"
        return "API 密钥"
    }

    function openManager(page) {
        manager.pageIndex = page
        manager.settingsKind = page === 0 ? "api" : ""
        manager.open()
    }

    function openSkillSettings(skillId) {
        if (skillId === "scheduled-tasks") manager.pageIndex = 2
        else if (skillId === "local-apps") manager.pageIndex = 1
        else manager.pageIndex = 0
        manager.settingsKind = skillId
        manager.open()
    }

    Connections {
        target: assistant
        function onConfirmationRequested(name, details, highRisk) {
            confirmationPopup.actionName = name
            confirmationPopup.details = details
            confirmationPopup.highRisk = highRisk
            rememberPermission.checked = !highRisk
            confirmationPopup.open()
        }
        function onAsrFallbackRequested(error) {
            asrFallbackPopup.errorText = error
            asrFallbackPopup.open()
        }
        function onShowWindowRequested() { window.show(); window.raise(); window.requestActivate() }
    }

    FileDialog {
        id: starrailFileDialog
        title: "选择三月七小助手程序"
        nameFilters: ["可执行程序 (*.exe)", "所有文件 (*)"]
        onAccepted: assistant.setStarrailExecutable(selectedFile)
    }

    Popup {
        id: confirmationPopup
        anchors.centerIn: Overlay.overlay
        width: Math.min(560, window.width - 80)
        modal: true
        closePolicy: Popup.NoAutoClose
        property string actionName: ""
        property string details: ""
        property bool highRisk: false
        background: Rectangle { color: "white"; radius: 26; border.color: window.borderColor }
        contentItem: ColumnLayout {
            spacing: 14
            Label { text: confirmationPopup.highRisk ? "确认高风险操作" : "允许执行工具？"; font.pixelSize: 21; font.bold: true; color: window.textColor }
            Label { Layout.fillWidth: true; text: confirmationPopup.actionName; wrapMode: Text.Wrap; font.bold: true; color: confirmationPopup.highRisk ? "#D70015" : window.accent }
            TextArea { Layout.fillWidth: true; Layout.preferredHeight: 160; text: confirmationPopup.details; readOnly: true; wrapMode: TextEdit.Wrap; color: window.secondaryText; background: Rectangle { color: "#F7F7F9"; radius: 12 } }
            CheckBox { id: rememberPermission; visible: !confirmationPopup.highRisk; text: "记住这个具体动作"; checked: true }
            Label { visible: confirmationPopup.highRisk; text: "此类操作每次都需要确认，不会保存授权。"; color: "#D70015" }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                SoftButton { text: "拒绝"; onClicked: { confirmationPopup.close(); assistant.resolveConfirmation(false, false) } }
                BlueButton { text: confirmationPopup.highRisk ? "确认本次" : "允许"; onClicked: { confirmationPopup.close(); assistant.resolveConfirmation(true, rememberPermission.checked) } }
            }
        }
    }

    Popup {
        id: asrFallbackPopup
        anchors.centerIn: Overlay.overlay
        width: Math.min(540, window.width - 80)
        modal: true
        closePolicy: Popup.NoAutoClose
        property string errorText: ""
        background: Rectangle { color: "white"; radius: 26; border.color: window.borderColor }
        contentItem: ColumnLayout {
            spacing: 14
            Label { text: "API 转写暂时不可用"; font.pixelSize: 21; font.bold: true; color: window.textColor }
            Label { Layout.fillWidth: true; text: "是否仅本次加载本地 ASR 完成这段录音？完成后会立即卸载。"; wrapMode: Text.Wrap; color: window.secondaryText }
            Label { Layout.fillWidth: true; text: asrFallbackPopup.errorText; wrapMode: Text.Wrap; font.pixelSize: 11; color: "#8E8E93" }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                SoftButton { text: "不用"; onClicked: { asrFallbackPopup.close(); assistant.resolveAsrFallback(false) } }
                BlueButton { text: "仅本次使用"; onClicked: { asrFallbackPopup.close(); assistant.resolveAsrFallback(true) } }
            }
        }
    }

    Popup {
        id: manager
        anchors.centerIn: Overlay.overlay
        width: Math.min(manager.pageIndex === 1 || manager.pageIndex === 2 ? 760 : (manager.settingsKind === "desk-lamp" ? 640 : 620), window.width - 70)
        height: Math.min(
            manager.pageIndex === 1 ? 620
            : (manager.pageIndex === 2 ? 650
            : (manager.pageIndex === 3 ? 520
            : (manager.settingsKind === "continuous-conversation" ? 300
            : (manager.settingsKind === "desk-lamp" ? 520
            : (manager.settingsKind === "starrail-dailies" ? 410
            : (manager.settingsKind === "api" ? 470 : 460)))))),
            window.height - 50
        )
        modal: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property int pageIndex: 0
        property string settingsKind: "api"
        property string permissionCategory: "全部"
        background: Rectangle { color: window.pageBackground; radius: 30; border.color: window.borderColor }
        contentItem: ColumnLayout {
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                SettingTitle { text: window.managerTitle() }
                Item { Layout.fillWidth: true }
                Button {
                    implicitWidth: 32
                    implicitHeight: 32
                    text: "×"
                    font.pixelSize: 20
                    contentItem: Text { text: parent.text; color: window.secondaryText; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font }
                    background: Rectangle { radius: height / 2; color: parent.hovered ? "#E2E2E7" : "transparent" }
                    onClicked: manager.close()
                }
            }
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: manager.pageIndex

                ScrollView {
                    clip: true
                    ColumnLayout {
                        width: Math.max(100, manager.availableWidth - 18)
                        spacing: 14
                        Label {
                            visible: manager.settingsKind === "api"
                            Layout.fillWidth: true
                            text: "输入内容只保存在本机 api_key.txt；留空会保留原值，页面不会显示已有密钥。"
                            wrapMode: Text.Wrap
                            color: window.secondaryText
                            font.pixelSize: 11
                        }
                        Label {
                            visible: manager.settingsKind === "wake-word"
                            Layout.fillWidth: true
                            text: "用一个或多个自定义词唤醒猫猫，并在需要时打断播报。"
                            wrapMode: Text.Wrap
                            color: window.secondaryText
                        }
                        Label {
                            visible: manager.settingsKind === "continuous-conversation"
                            Layout.fillWidth: true
                            text: "开启后，猫猫会在回答结束后继续听你说话，直到你说再见或没事。"
                            wrapMode: Text.Wrap
                            color: window.secondaryText
                        }
                        Label {
                            visible: manager.settingsKind === "desk-lamp"
                            Layout.fillWidth: true
                            text: "连接米家设备后，猫猫可以在局域网内控制家具并读取设备状态。"
                            wrapMode: Text.Wrap
                            color: window.secondaryText
                        }
                        Label {
                            visible: manager.settingsKind === "starrail-dailies"
                            Layout.fillWidth: true
                            text: "定位三月七小助手后，猫猫可以启动它并点击“完整运行”。"
                            wrapMode: Text.Wrap
                            color: window.secondaryText
                        }
                        Card {
                            visible: manager.settingsKind === "api"
                            Layout.fillWidth: true; implicitHeight: 310; radius: 30
                            ColumnLayout {
                                id: apiSettings; anchors.fill: parent; anchors.margins: 22; spacing: 7
                                Label { text: "Kimi API Key  ·  " + (assistant.keyStatus.kimi_key ? "已配置" : "未配置"); color: window.textColor; font.bold: true }
                                TextField { id: kimiKey; Layout.fillWidth: true; implicitHeight: 38; placeholderText: "输入新的 Key（留空则不修改）"; echoMode: TextInput.Password; background: Rectangle { radius: 19; color: "#F0F0F3" } }
                                Label { text: "MiMo API Key  ·  " + (assistant.keyStatus.mimo_key ? "已配置" : "未配置"); color: window.textColor; font.bold: true; Layout.topMargin: 5 }
                                TextField { id: mimoKey; Layout.fillWidth: true; implicitHeight: 38; placeholderText: "输入新的 Key（留空则不修改）"; echoMode: TextInput.Password; background: Rectangle { radius: 19; color: "#F0F0F3" } }
                                BlueButton { Layout.fillWidth: true; implicitHeight: 40; text: "保存"; Layout.topMargin: 8; onClicked: { assistant.saveApiKeys(kimiKey.text, mimoKey.text); kimiKey.clear(); mimoKey.clear() } }
                            }
                        }
                        Card {
                            visible: manager.settingsKind === "wake-word" || manager.settingsKind === "continuous-conversation"
                            Layout.fillWidth: true
                            implicitHeight: manager.settingsKind === "continuous-conversation" ? 100 : 275
                            radius: 28
                            ColumnLayout {
                                id: voiceSettings; anchors.fill: parent; anchors.margins: 22; spacing: 10
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { Layout.fillWidth: true; text: manager.settingsKind === "continuous-conversation" ? "回答后继续聆听" : "启用本地唤醒监听"; color: window.textColor; font.bold: true }
                                    BlueSwitch { visible: manager.settingsKind === "continuous-conversation"; checked: assistant.continuousEnabled; onToggled: assistant.setContinuousEnabled(checked) }
                                    BlueSwitch { visible: manager.settingsKind === "wake-word"; checked: assistant.wakeEnabled; onToggled: assistant.setWakeEnabled(checked) }
                                }
                                Label {
                                    visible: manager.settingsKind === "wake-word"
                                    Layout.fillWidth: true
                                    text: !assistant.wakeEnabled ? "已关闭，不会监听环境声音。手动录音仍可使用。" : (assistant.wakeEnrolled ? "已开启 · 正在监听“" + assistant.wakeWords + "”" : "已开启 · 请先录制唤醒词")
                                    color: assistant.wakeEnabled ? "#34C759" : "#8E8E93"
                                }
                                Label { visible: manager.settingsKind === "wake-word"; text: "唤醒词（多个词用逗号分隔）"; color: window.secondaryText; font.pixelSize: 11 }
                                TextField {
                                    id: wakeWordsField
                                    visible: manager.settingsKind === "wake-word"
                                    Layout.fillWidth: true
                                    implicitHeight: 38
                                    text: assistant.wakeWords
                                    enabled: assistant.wakeEnabled
                                    background: Rectangle { radius: 19; color: "#F0F0F3" }
                                }
                                RowLayout {
                                    visible: manager.settingsKind === "wake-word"
                                    Layout.fillWidth: true
                                    SoftButton { Layout.fillWidth: true; text: "保存唤醒词"; enabled: assistant.wakeEnabled; onClicked: assistant.saveWakeWords(wakeWordsField.text) }
                                    BlueButton { Layout.fillWidth: true; text: assistant.wakeEnrolled ? "重新录制唤醒词" : "录制唤醒词"; enabled: assistant.wakeEnabled; onClicked: assistant.enrollWakeWords() }
                                }
                            }
                        }
                        Card {
                            visible: manager.settingsKind === "desk-lamp" || manager.settingsKind === "starrail-dailies"
                            Layout.fillWidth: true
                            implicitHeight: manager.settingsKind === "desk-lamp" ? 330 : 245
                            radius: 28
                            ColumnLayout {
                                id: integrations; anchors.fill: parent; anchors.margins: 22; spacing: 10
                                Label {
                                    visible: manager.settingsKind === "desk-lamp"
                                    text: assistant.integrationStatus.xiaomiConfigured ? "已连接的家具 · " + assistant.integrationStatus.xiaomiDevices.length : "尚未连接家具"
                                    color: assistant.integrationStatus.xiaomiConfigured ? "#34C759" : "#8E8E93"
                                    font.bold: true
                                }
                                Repeater {
                                    model: manager.settingsKind === "desk-lamp" ? assistant.integrationStatus.xiaomiDevices : []
                                    delegate: Rectangle {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        implicitHeight: 68
                                        radius: 18
                                        color: "#F7F7F9"
                                        border.color: window.borderColor
                                        ColumnLayout {
                                            anchors.fill: parent; anchors.margins: 12; spacing: 2
                                            Label { text: modelData.name || "米家设备"; color: window.textColor; font.bold: true }
                                            Label { text: (modelData.model || "未知型号") + " · " + (modelData.ip || "IP 未知"); color: window.secondaryText; font.pixelSize: 10 }
                                        }
                                    }
                                }
                                Label { visible: manager.settingsKind === "desk-lamp" && !assistant.integrationStatus.xiaomiConfigured; text: "连接设备后，它会显示在这里。"; color: "#8E8E93"; Layout.fillWidth: true }
                                Item { Layout.fillHeight: true }
                                BlueButton { visible: manager.settingsKind === "desk-lamp"; Layout.fillWidth: true; text: "连接或更换设备"; onClicked: assistant.launchXiaomiSetup() }
                                SoftButton { visible: manager.settingsKind === "desk-lamp"; Layout.fillWidth: true; text: "刷新设备列表"; onClicked: assistant.refreshIntegrations() }
                                Label {
                                    visible: manager.settingsKind === "starrail-dailies"
                                    Layout.fillWidth: true
                                    text: assistant.integrationStatus.starrailExecutableConfigured ? "已定位：" + assistant.integrationStatus.starrailExecutable : "尚未定位三月七小助手。"
                                    wrapMode: Text.Wrap
                                    color: assistant.integrationStatus.starrailExecutableConfigured ? "#34C759" : "#8E8E93"
                                }
                                Item { Layout.fillHeight: true }
                                AccentSoftButton { visible: manager.settingsKind === "starrail-dailies"; Layout.fillWidth: true; implicitHeight: 40; text: "打开项目页面  ↗"; onClicked: assistant.openStarrailProject() }
                                BlueButton { visible: manager.settingsKind === "starrail-dailies"; Layout.fillWidth: true; implicitHeight: 40; text: "定位已安装的 March7th Launcher.exe"; onClicked: starrailFileDialog.open() }
                            }
                        }
                    }
                }

                ColumnLayout {
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: "普通操作确认一次后会记住；高风险操作仍会每次确认。删除后下次会重新询问。"; color: window.secondaryText; font.pixelSize: 12 }
                        Label { text: "共 " + assistant.permissions.length + " 个"; color: window.accent; font.bold: true; leftPadding: 12; rightPadding: 12; topPadding: 7; bottomPadding: 7; background: Rectangle { radius: height / 2; color: "#E8F2FF" } }
                    }
                    GridLayout {
                        Layout.fillWidth: true; columns: 3; columnSpacing: 8; rowSpacing: 6
                        Repeater {
                            model: ["全部", "网站", "应用与窗口", "自动化", "常用动作", "其他"]
                            delegate: Button {
                                required property string modelData
                                Layout.fillWidth: true; implicitHeight: 30; text: modelData; font.pixelSize: 11
                                contentItem: Text { text: parent.text; color: manager.permissionCategory === parent.text ? "white" : "#3A3A3C"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font }
                                background: Rectangle { radius: 15; color: manager.permissionCategory === parent.text ? window.accent : "#ECECF0" }
                                onClicked: manager.permissionCategory = text
                            }
                        }
                    }
                    ListView {
                        id: permissionView
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                        model: assistant.permissions.filter(item => manager.permissionCategory === "全部" || item.category === manager.permissionCategory)
                        delegate: Rectangle {
                            required property var modelData
                            width: permissionView.width; height: 76; radius: 24; color: window.cardColor; border.color: window.borderColor
                            RowLayout { anchors.fill: parent; anchors.margins: 16; ColumnLayout { Layout.fillWidth: true; Label { text: modelData.label; font.bold: true; color: window.textColor } Label { text: modelData.category + " · " + modelData.created_at; font.pixelSize: 11; color: window.secondaryText } } DangerButton { implicitWidth: 62; text: "删除"; onClicked: assistant.deletePermission(modelData.key) } }
                        }
                        Label { anchors.centerIn: parent; visible: permissionView.count === 0; text: "这里还没有记住的权限"; color: window.secondaryText }
                    }
                }

                ColumnLayout {
                    spacing: 10
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; text: "猫猫在运行或缩到托盘时会按本机时间执行；普通任务也不会额外语音通知。"; color: window.secondaryText; font.pixelSize: 12 }
                        Label { text: "启用 " + assistant.schedules.filter(item => item.enabled).length + " / 共 " + assistant.schedules.length + " 个"; color: window.accent; font.bold: true; leftPadding: 12; rightPadding: 12; topPadding: 7; bottomPadding: 7; background: Rectangle { radius: height / 2; color: "#E8F2FF" } }
                    }
                    Card {
                        Layout.fillWidth: true; implicitHeight: 230; radius: 24
                        GridLayout {
                            anchors.fill: parent; anchors.margins: 18; columns: 2; columnSpacing: 16; rowSpacing: 6
                            Label { Layout.columnSpan: 2; text: "任务内容"; color: window.secondaryText; font.pixelSize: 11 }
                            TextField { id: scheduleCommand; Layout.columnSpan: 2; Layout.fillWidth: true; implicitHeight: 38; placeholderText: "例如：帮我过星铁日常"; background: Rectangle { radius: 19; color: "#F0F0F3" } }
                            Label { text: "首次执行（YYYY-MM-DD HH:MM）"; color: window.secondaryText; font.pixelSize: 11; Layout.topMargin: 5 }
                            Label { text: "重复方式"; color: window.secondaryText; font.pixelSize: 11; Layout.topMargin: 5 }
                            TextField { id: scheduleTime; Layout.fillWidth: true; implicitHeight: 36; text: assistant.defaultScheduleTime; background: Rectangle { radius: 18; color: "#F0F0F3" } }
                            CompactCombo { id: scheduleRepeat; Layout.fillWidth: true; implicitHeight: 36; model: ["仅一次", "每天"]; currentIndex: 1 }
                            BlueSwitch { id: scheduleSilent; text: "静默执行（不播报、不弹出窗口）"; checked: true; Layout.topMargin: 8 }
                            BlueButton { implicitWidth: 120; implicitHeight: 38; text: "添加任务"; Layout.alignment: Qt.AlignRight; Layout.topMargin: 8; onClicked: { assistant.createSchedule(scheduleCommand.text, scheduleTime.text, scheduleRepeat.currentText === "每天", scheduleSilent.checked); scheduleCommand.clear() } }
                        }
                    }
                    Card {
                        Layout.fillWidth: true; Layout.fillHeight: true; color: "transparent"; border.width: 0
                        ListView {
                            id: scheduleView
                            anchors.fill: parent; clip: true; spacing: 8; model: assistant.schedules
                            delegate: Rectangle {
                                required property var modelData
                                width: scheduleView.width; height: 82; radius: 24; color: window.cardColor; border.color: window.borderColor
                                RowLayout { anchors.fill: parent; anchors.margins: 15; ColumnLayout { Layout.fillWidth: true; Label { text: "#" + modelData.id + "  " + modelData.command; font.bold: true; color: window.textColor } Label { text: modelData.repeatLabel + " · " + modelData.displayTime + " · " + modelData.modeLabel + " · " + modelData.statusLabel; font.pixelSize: 11; color: window.secondaryText } } SoftButton { visible: modelData.enabled; implicitWidth: 58; text: "停用"; onClicked: assistant.cancelSchedule(modelData.id) } DangerButton { implicitWidth: 58; text: "删除"; onClicked: assistant.deleteSchedule(modelData.id) } }
                            }
                            Label { anchors.centerIn: parent; visible: scheduleView.count === 0; text: "还没有定时任务"; color: window.secondaryText }
                        }
                    }
                }

                ColumnLayout {
                    spacing: 14
                    Card {
                        Layout.fillWidth: true; implicitHeight: 145; radius: 38
                        ColumnLayout {
                            anchors.fill: parent; anchors.margins: 26; spacing: 3
                            Label { text: "喵~"; font.pixelSize: 34; font.bold: true; color: window.textColor }
                            Label { text: "住在你电脑里的语音助手喵~"; font.pixelSize: 14; color: window.secondaryText }
                        }
                    }
                    Card {
                        Layout.fillWidth: true; Layout.fillHeight: true; radius: 30
                        GridLayout {
                            anchors.fill: parent; anchors.margins: 24; columns: 2; columnSpacing: 18; rowSpacing: 15
                            Label { text: "生日"; color: "#8E8E93"; font.pixelSize: 11 }
                            Label { text: "2026 年 8 月 9 日"; color: window.textColor; font.bold: true }
                            Label { text: "开发者"; color: "#8E8E93"; font.pixelSize: 11 }
                            Label { text: "MaoMao contributors"; color: window.textColor; font.bold: true }
                            Label { text: "会做什么"; color: "#8E8E93"; font.pixelSize: 11 }
                            Label { Layout.fillWidth: true; text: "语音对话、本地记忆、电脑操作、定时任务与可管理技能"; wrapMode: Text.Wrap; color: window.textColor }
                            Label { text: "隐私"; color: "#8E8E93"; font.pixelSize: 11 }
                            Label { Layout.fillWidth: true; text: "记忆与权限保存在本机；敏感操作仍由你确认"; wrapMode: Text.Wrap; color: window.textColor }
                        }
                    }
                }
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 22
        spacing: 0

        Item {
            id: skillSlot
            Layout.preferredWidth: window.skillOpen ? 274 : 28
            Layout.fillHeight: true
            clip: true
            Behavior on Layout.preferredWidth { NumberAnimation { duration: 130; easing.type: Easing.OutCubic } }

            Card {
                id: skillPanel
                width: 238
                height: parent.height
                visible: skillSlot.width > 80
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 8
                    RowLayout { Layout.fillWidth: true; Layout.leftMargin: 6; Layout.rightMargin: 5; Label { text: "技能"; font.pixelSize: 20; font.bold: true; color: window.textColor } Item { Layout.fillWidth: true } Label { text: assistant.skills.filter(item => item.enabled).length + "/" + assistant.skills.length; color: window.secondaryText; font.pixelSize: 10 } }
                    Rectangle {
                        Layout.fillWidth: true; implicitHeight: 38; radius: 19; color: "#F0F0F3"
                        RowLayout { anchors.fill: parent; anchors.margins: 2; spacing: 2
                            Button { Layout.fillWidth: true; implicitHeight: 34; text: "已学习"; font.pixelSize: 11; font.bold: true; contentItem: Text { text: parent.text; color: window.skillMode === "learned" ? "white" : "#3A3A3C"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font } background: Rectangle { radius: 17; color: window.skillMode === "learned" ? window.accent : "transparent" } onClicked: window.skillMode = "learned" }
                            Button { Layout.fillWidth: true; implicitHeight: 34; text: "技能库"; font.pixelSize: 11; font.bold: true; contentItem: Text { text: parent.text; color: window.skillMode === "library" ? "white" : "#3A3A3C"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font } background: Rectangle { radius: 17; color: window.skillMode === "library" ? window.accent : "transparent" } onClicked: window.skillMode = "library" }
                        }
                    }
                    GridLayout {
                        Layout.fillWidth: true; columns: 2; columnSpacing: 2; rowSpacing: 2
                        Repeater {
                            model: ["全部", "语音与对话", "电脑与浏览器", "屏幕与视觉", "记忆与文件", "设备与自动化", "基础能力"]
                            delegate: Button { required property string modelData; Layout.fillWidth: true; implicitHeight: 27; text: modelData; font.pixelSize: 9; contentItem: Text { text: parent.text; color: window.skillCategory === parent.text ? window.accent : window.secondaryText; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font } background: Rectangle { radius: 14; color: window.skillCategory === parent.text ? "#E8F2FF" : "#FFFFFF" } onClicked: window.skillCategory = text }
                        }
                    }
                    ListView {
                        id: skillList
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                        model: assistant.skills.filter(item => item.enabled === (window.skillMode === "learned") && (window.skillCategory === "全部" || item.category === window.skillCategory))
                        delegate: Rectangle {
                            required property var modelData
                                width: skillList.width; height: modelData.hasSettings ? 112 : 84; radius: 18; color: "#F7F7F9"; border.color: window.borderColor
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 10; spacing: 2
                                RowLayout { Layout.fillWidth: true; Label { Layout.fillWidth: true; text: modelData.name; font.pixelSize: 12; font.bold: true; color: window.textColor } StarButton { text: modelData.favorite ? "★" : "☆"; onClicked: assistant.setSkillFavorite(modelData.id, !modelData.favorite) } BlueSwitch { checked: modelData.enabled; onToggled: assistant.setSkillEnabled(modelData.id, checked) } }
                                Label { Layout.fillWidth: true; text: modelData.description; maximumLineCount: 2; elide: Text.ElideRight; wrapMode: Text.Wrap; font.pixelSize: 9; color: window.secondaryText }
                                SoftButton { visible: modelData.hasSettings; text: "设置"; Layout.alignment: Qt.AlignLeft; onClicked: window.openSkillSettings(modelData.id) }
                            }
                        }
                        Label { anchors.centerIn: parent; visible: skillList.count === 0; text: window.skillMode === "learned" ? "这个分类还没有已学习技能" : "这个分类没有可学习技能"; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter; color: "#8E8E93"; width: parent.width - 20 }
                    }
                }
            }
            Button {
                width: window.skillOpen ? 32 : 28; height: 118
                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                text: window.skillOpen ? "❮" : "技\n能\n栏"
                font.pixelSize: window.skillOpen ? 24 : 12; font.bold: true
                contentItem: Text { text: parent.text; color: window.accent; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font }
                background: Rectangle { radius: 14; color: parent.hovered ? "#D8E9FF" : "#E8F2FF" }
                onClicked: { window.skillOpen = !window.skillOpen; assistant.setSkillSidebarExpanded(window.skillOpen) }
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 14
            Layout.rightMargin: 14

            ColumnLayout {
                anchors.fill: parent
                spacing: 0
                RowLayout {
                    Layout.fillWidth: true; implicitHeight: 34
                    Label { text: "喵~"; font.pixelSize: 27; font.bold: true; color: window.textColor }
                    Item { Layout.fillWidth: true }
                    SoftButton { text: "关于"; onClicked: window.openManager(3) }
                    AccentSoftButton { implicitWidth: 78; implicitHeight: 28; text: "API 密钥"; onClicked: window.openManager(0) }
                    Label { text: assistant.usage; color: window.secondaryText; leftPadding: 13; rightPadding: 13; topPadding: 7; bottomPadding: 7; background: Rectangle { color: "#E9E9ED"; radius: height / 2 } }
                }
                RowLayout {
                    Layout.fillWidth: true; implicitHeight: 42; Layout.bottomMargin: 14
                    Label { text: "文本模型"; color: window.secondaryText; font.pixelSize: 12 }
                    CompactCombo { implicitWidth: 174; textRole: "label"; valueRole: "value"; model: assistant.modelOptions; Component.onCompleted: currentIndex = window.comboIndex(assistant.modelOptions, assistant.modelMode); onActivated: assistant.setModelMode(currentValue) }
                    Label { text: "语音转写"; color: window.secondaryText; font.pixelSize: 12; Layout.leftMargin: 12 }
                    CompactCombo { implicitWidth: 178; textRole: "label"; valueRole: "value"; model: assistant.asrOptions; Component.onCompleted: currentIndex = window.comboIndex(assistant.asrOptions, assistant.asrMode); onActivated: assistant.setAsrMode(currentValue) }
                    Item { Layout.fillWidth: true }
                }
                Card {
                    Layout.fillWidth: true; Layout.fillHeight: true; radius: 42
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 20; spacing: 6
                        RowLayout { Layout.fillWidth: true; Item { Layout.fillWidth: true } SoftButton { text: "清除记录"; onClicked: assistant.clearConversation() } }
                        ListView {
                            id: chatView
                            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 12; model: assistant.messages
                            onCountChanged: positionViewAtEnd()
                            delegate: ColumnLayout {
                                required property var modelData
                                width: chatView.width; spacing: 3
                                Label { text: modelData.role === "user" ? "你" : (modelData.role === "assistant" ? "猫猫" : "系统"); font.pixelSize: 13; font.bold: modelData.role === "user"; color: modelData.role === "user" ? window.accent : (modelData.role === "system" ? window.secondaryText : window.textColor) }
                                Label { Layout.fillWidth: true; text: modelData.text; wrapMode: Text.Wrap; font.pixelSize: 13; color: window.textColor }
                                Label { visible: modelData.meta !== ""; text: modelData.meta; font.pixelSize: 11; color: "#8E8E93" }
                            }
                            Label { anchors.centerIn: parent; visible: chatView.count === 0; text: "有什么想和猫猫聊的吗？"; color: window.secondaryText; font.pixelSize: 14 }
                        }
                    }
                }
                Card {
                    Layout.fillWidth: true; implicitHeight: 150; radius: 42; Layout.topMargin: 14; Layout.bottomMargin: 10
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 18; spacing: 4
                        RowLayout {
                            Layout.fillWidth: true; spacing: 10
                            ColumnLayout { spacing: 4; Label { text: "输入"; color: window.secondaryText; font.pixelSize: 12 } BlueButton { implicitWidth: 120; text: assistant.recording ? "■  结束录音" : "●  开始录音"; enabled: !assistant.busy || assistant.recording; onClicked: assistant.toggleRecording() } }
                            ColumnLayout { spacing: 4; Label { text: "音色"; color: window.secondaryText; font.pixelSize: 12 } CompactCombo { implicitWidth: 104; implicitHeight: 42; model: assistant.voiceOptions; Component.onCompleted: currentIndex = Math.max(0, assistant.voiceOptions.indexOf(assistant.voice)); onActivated: assistant.setVoice(currentText) } }
                            ColumnLayout { spacing: 4; Label { text: "语音生成"; color: window.secondaryText; font.pixelSize: 12 } CompactCombo { implicitWidth: 258; implicitHeight: 42; textRole: "label"; valueRole: "value"; model: assistant.ttsEngineOptions; Component.onCompleted: currentIndex = window.comboIndex(assistant.ttsEngineOptions, assistant.ttsEngine); onActivated: assistant.setTtsEngine(currentValue) } }
                            ColumnLayout {
                                spacing: 4
                                Label { text: "预加载"; color: window.secondaryText; font.pixelSize: 12 }
                                Button {
                                    implicitWidth: 72
                                    implicitHeight: 42
                                    text: assistant.preloadLoading ? "加载中" : (assistant.preloadEnabled ? "开" : "关")
                                    enabled: !assistant.preloadLoading
                                    font.pixelSize: 12
                                    contentItem: Text { text: parent.text; color: assistant.preloadLoading ? "#A35A00" : (assistant.preloadEnabled ? window.accent : window.secondaryText); horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font }
                                    background: Rectangle { radius: height / 2; color: assistant.preloadLoading ? "#FFF2D8" : (assistant.preloadEnabled ? (parent.hovered ? "#D8E9FF" : "#E8F2FF") : (parent.hovered ? "#E2E2E7" : "#F0F0F3")) }
                                    onClicked: assistant.setPreloadEnabled(!assistant.preloadEnabled)
                                }
                            }
                            Item { Layout.fillWidth: true }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: "播放"; color: window.secondaryText; font.pixelSize: 12; Layout.rightMargin: 5 }
                            SoftButton { implicitWidth: 62; text: "暂停"; onClicked: assistant.ttsControl("pause") }
                            SoftButton { implicitWidth: 62; text: "继续"; onClicked: assistant.ttsControl("resume") }
                            SoftButton { implicitWidth: 62; text: "停止"; onClicked: assistant.ttsControl("stop") }
                            Item { Layout.fillWidth: true }
                            DangerButton { implicitWidth: 82; text: "暂停全部"; onClicked: assistant.pauseAll() }
                        }
                        RowLayout { Layout.fillWidth: true; Label { text: "●"; color: assistant.busy ? "#FF9F0A" : "#34C759" } Label { text: assistant.status; color: window.secondaryText; font.pixelSize: 12 } Item { Layout.fillWidth: true } }
                    }
                }
                Card {
                    Layout.fillWidth: true; implicitHeight: 118; radius: 42
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 27
                        anchors.rightMargin: 22
                        anchors.topMargin: 16
                        anchors.bottomMargin: 16
                        spacing: 12
                        TextArea { id: input; Layout.fillWidth: true; Layout.fillHeight: true; placeholderText: "输入消息，Ctrl + Enter 发送"; wrapMode: TextEdit.Wrap; color: window.textColor; background: Rectangle { color: window.cardColor } Keys.onPressed: event => { if (event.key === Qt.Key_Return && (event.modifiers & Qt.ControlModifier)) { send(); event.accepted = true } } function send() { const value = text.trim(); if (value !== "" && !assistant.busy) { assistant.submit(value); text = "" } } }
                        BlueButton { implicitWidth: 112; implicitHeight: 44; text: assistant.busy ? "处理中" : "发送"; enabled: !assistant.busy; onClicked: input.send() }
                    }
                }
            }
        }

        Item {
            id: favoriteSlot
            Layout.preferredWidth: window.favoriteOpen ? 240 : 28
            Layout.fillHeight: true
            clip: true
            Behavior on Layout.preferredWidth { NumberAnimation { duration: 130; easing.type: Easing.OutCubic } }
            Button {
                width: window.favoriteOpen ? 32 : 28; height: 118
                anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                text: window.favoriteOpen ? "❯" : "收\n藏\n夹"
                font.pixelSize: window.favoriteOpen ? 24 : 12; font.bold: true
                contentItem: Text { text: parent.text; color: "#A66300"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font: parent.font }
                background: Rectangle { radius: 14; color: parent.hovered ? "#FFE9B5" : "#FFF3D9" }
                onClicked: { window.favoriteOpen = !window.favoriteOpen; assistant.setFavoriteSidebarExpanded(window.favoriteOpen) }
            }
            Card {
                x: 32; width: 208; height: parent.height
                visible: favoriteSlot.width > 80
                ColumnLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 8
                    RowLayout { Layout.fillWidth: true; Layout.leftMargin: 4; Label { text: "收藏夹"; font.pixelSize: 20; font.bold: true; color: window.textColor } Item { Layout.fillWidth: true } Label { text: assistant.skills.filter(item => item.favorite).length; color: window.secondaryText; font.pixelSize: 10 } }
                    ListView {
                        id: favoriteList
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                        model: assistant.skills.filter(item => item.favorite)
                        delegate: Rectangle {
                            required property var modelData
                            width: favoriteList.width; height: 88; radius: 17; color: "#F7F7F9"; border.color: window.borderColor
                            ColumnLayout { anchors.fill: parent; anchors.margins: 10; spacing: 3; RowLayout { Layout.fillWidth: true; Label { Layout.fillWidth: true; text: modelData.name; font.pixelSize: 11; font.bold: true; color: window.textColor } BlueSwitch { checked: modelData.enabled; onToggled: assistant.setSkillEnabled(modelData.id, checked) } } RowLayout { Layout.fillWidth: true; SoftButton { visible: modelData.hasSettings; text: "设置"; onClicked: window.openSkillSettings(modelData.id) } Item { Layout.fillWidth: true } Button { implicitWidth: 28; implicitHeight: 26; text: "★"; contentItem: Text { text: parent.text; color: "#F5A623"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter } background: Rectangle { radius: 13; color: "#FFF3D9" } onClicked: assistant.setSkillFavorite(modelData.id, false) } } }
                        }
                        Label { anchors.centerIn: parent; visible: favoriteList.count === 0; width: parent.width - 20; text: "在技能栏点击 ☆\n即可收藏常用技能"; horizontalAlignment: Text.AlignHCenter; color: "#8E8E93"; font.pixelSize: 10 }
                    }
                }
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        visible: !assistant.startupReady
        color: window.pageBackground
        z: 100
        ColumnLayout {
            anchors.centerIn: parent; width: Math.min(520, parent.width - 80); spacing: 16
            Label { text: "准备猫猫"; font.pixelSize: 25; font.bold: true; color: window.textColor; Layout.alignment: Qt.AlignHCenter }
            Label { Layout.fillWidth: true; text: assistant.startupMessage; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter; color: window.secondaryText }
            ProgressBar { Layout.fillWidth: true; from: 0; to: 100; value: assistant.startupProgress }
            BlueButton { text: assistant.startupProgress > 0 ? "正在安装" : "安装所需组件"; enabled: assistant.startupProgress === 0; Layout.alignment: Qt.AlignHCenter; onClicked: assistant.installStartupComponents() }
        }
    }
}
