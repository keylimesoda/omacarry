import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: win
    width: 1000; height: 760; minimumWidth: 680; minimumHeight: 500
    visible: true; title: qsTr("omacarry"); color: "#101012"
    Material.theme: Material.Dark
    Material.accent: theme.accent
    Material.roundedScale: theme.radius
    property string expandedId: ""
    property var expandedPlugin: ({})
    property var choices: []
    property string choicesError: ""
    property bool showOther: false
    property bool helpVisible: false
    property string githubLogin: ""
    property string forkSuggestion: ""

    function send(action, plugin, extra) {
        let r = extra || {}; r.action = action; r.plugin = plugin || ""; backend.request(r)
    }
    function refresh() { send("scan") }
    function expand(p) {
        if (expandedId === p.id) { expandedId = ""; return }
        expandedId = p.id; expandedPlugin = p; choices = []; choicesError = ""
        if (!p.tracked) send("prs", p.id)
    }
    function track(p) {
        githubLogin = ""; forkSuggestion = ""
        let t = p.tracked || {}
        let ref = p.upstream || ""
        let origin = p.remotes && p.upstream_remote ? p.remotes[p.upstream_remote] : ""
        let match = (origin || p.candidate || "").match(/github\.com[:/]([^/]+\/[^/]+?)(?:\.git)?\/?$/i)
        trackUpstream.text = t.upstream || (match ? match[1] : "")
        trackBase.text = t.base || (ref.indexOf("/") >= 0 ? ref.substring(ref.indexOf("/") + 1) : "")
        let fork = p.remotes ? (p.remotes.fork || (p.upstream_remote !== "origin" ? p.remotes.origin : "")) : ""
        let fm = (fork || "").match(/github\.com[:/]([^/]+\/[^/]+?)(?:\.git)?\/?$/i)
        trackRepo.text = t.head_repo || (fm ? fm[1] : "")
        trackHead.text = t.head || p.branch || ""
        trackError.text = ""; trackDialog.open()
        if (!trackRepo.text) send("login", p.id)
    }
    function suggestFork() {
        if (!githubLogin || (trackRepo.text && trackRepo.text !== forkSuggestion)) return
        let match = trackUpstream.text.trim().match(/^(?:https:\/\/github\.com\/)?[A-Za-z0-9_.-]+\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/i)
        if (match) { forkSuggestion = githubLogin + "/" + match[1]; trackRepo.text = forkSuggestion }
    }
    Component.onCompleted: refresh()
    Shortcut { sequence: "Q"; context: Qt.ApplicationShortcut; enabled: !trackDialog.opened && !upstreamDialog.opened && !helpDialog.opened; onActivated: Qt.quit() }
    Shortcut { sequence: "?"; context: Qt.ApplicationShortcut; onActivated: helpDialog.open() }
    Shortcut { sequence: "Ctrl+R"; context: Qt.ApplicationShortcut; onActivated: refresh() }
    Connections {
        target: backend
        function onPluginsChanged() {
            for (let p of backend.plugins) if (p.id === win.expandedId) { win.expandedPlugin = p; break }
        }
        function onCompleted(action, result) {
            if (action === "prs" && result._plugin === win.expandedId) { win.choices = result.choices || []; win.choicesError = result.ok ? "" : result.message }
            else if (action === "login" && trackDialog.opened && result._plugin === win.expandedId) {
                win.githubLogin = result.login || ""; win.suggestFork()
            }
            else if (action === "track") { if (result.ok) { trackDialog.close(); refresh() } else trackError.text = result.message }
            else if (action === "upstream") { if (result.ok) { upstreamDialog.close(); refresh() } else upstreamError.text = result.message }
            else if (["sync", "sync_all", "policy", "select", "clear_track", "hook"].indexOf(action) >= 0) refresh()
        }
    }
    header: ToolBar {
        background: Rectangle { color: "#1b1b1e" }
        RowLayout {
            LayoutMirroring.enabled: Qt.application.layoutDirection === Qt.RightToLeft
            LayoutMirroring.childrenInherit: true
            anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 20
            Label { text: qsTr("OMACARRY"); font.bold: true; font.pixelSize: 15; font.letterSpacing: 1.2; color: theme.accent }
            Item { Layout.fillWidth: true }
            BusyIndicator { running: backend.busy; visible: running; implicitWidth: 30; implicitHeight: 30 }
            ToolButton { text: qsTr("Refresh"); onClicked: refresh() }
            ToolButton { text: qsTr("Sync all"); enabled: !backend.busy; onClicked: send("sync_all") }
            ToolButton { text: qsTr("Set up hook"); onClicked: hookDialog.open() }
            ToolButton { text: qsTr("?"); onClicked: helpDialog.open() }
        }
    }
    ColumnLayout {
        LayoutMirroring.enabled: Qt.application.layoutDirection === Qt.RightToLeft
        LayoutMirroring.childrenInherit: true
        anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter
        anchors.topMargin: 28; anchors.bottomMargin: 28
        width: Math.min(parent.width - 56, 1000); spacing: 12
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                spacing: 2
                Label { text: qsTr("Plugin updates"); font.pixelSize: 25; font.bold: true }
                Label { text: qsTr("Keep your changes in step with upstream."); opacity: 0.65; font.pixelSize: 14 }
            }
            Item { Layout.fillWidth: true }
            CheckBox { text: qsTr("Other plugins"); checked: win.showOther; onToggled: win.showOther = checked }
        }
        Rectangle {
            Layout.fillWidth: true; visible: backend.message.length > 0; implicitHeight: notice.implicitHeight + 20
            color: "#29292d"; radius: Math.min(4, theme.radius)
            Label { id: notice; anchors.fill: parent; anchors.margins: 10; text: backend.message; wrapMode: Text.WordWrap }
        }
        ScrollView {
            id: pluginScroll
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            ColumnLayout {
                width: pluginScroll.availableWidth; spacing: 8
                Repeater {
                    model: backend.plugins
                    delegate: Rectangle {
                        id: card
                        required property var modelData
                        property var p: modelData
                        property bool expanded: win.expandedId === p.id
                        property var last: p.last || ({})
                        visible: p.attention || win.showOther
                        Layout.fillWidth: true; implicitHeight: visible ? content.implicitHeight + 24 : 0
                        color: "#202024"; radius: Math.min(4, theme.radius)
                        ColumnLayout {
                            id: content; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 12; spacing: 8
                            RowLayout {
                                Layout.fillWidth: true
                                ColumnLayout {
                                    Layout.fillWidth: true; spacing: 2
                                    Label { text: card.p.id; font.pixelSize: 16; font.bold: true; elide: Text.ElideRight; Layout.fillWidth: true }
                                    Label { text: card.last.message || qsTr("No sync attempted yet"); opacity: 0.65; elide: Text.ElideRight; Layout.fillWidth: true }
                                }
                                Label { text: card.last.ok === false ? qsTr("Needs attention") : !card.p.auto ? qsTr("Paused") : card.last.ok ? qsTr("Synced") : qsTr("Enabled"); color: card.last.ok === false ? "#efb95c" : theme.accent; font.bold: true }
                                ToolButton { text: card.expanded ? "−" : "+"; Accessible.name: card.expanded ? "Collapse" : "Expand"; onClicked: win.expand(card.p) }
                            }
                            ColumnLayout {
                                visible: card.expanded; Layout.fillWidth: true; spacing: 8
                                Rectangle { Layout.fillWidth: true; height: 1; color: "#414147" }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: qsTr("Automatic sync"); Layout.fillWidth: true }
                                    Switch { checked: card.p.auto; enabled: !backend.busy; onClicked: send("policy", card.p.id, {enabled: checked}) }
                                }
                                Label { visible: !!card.p.tracked; text: card.p.tracked ? "Tracking  " + card.p.tracked.upstream + ":" + card.p.tracked.base + "  ←  " + card.p.tracked.head_repo + ":" + card.p.tracked.head : ""; wrapMode: Text.Wrap; Layout.fillWidth: true }
                                RowLayout {
                                    Layout.fillWidth: true; visible: !card.p.tracked
                                    ComboBox {
                                        id: prCombo; Layout.fillWidth: true
                                        model: win.choices.map(p => p.upstream + ":" + p.base + "  ←  " + p.head_repo + ":" + p.head + "  ·  " + p.prs.length + " PR(s)")
                                        currentIndex: win.choices.findIndex(p => p.key === card.p.selected) >= 0 ? win.choices.findIndex(p => p.key === card.p.selected) : (win.choices.length === 1 ? 0 : -1)
                                        displayText: currentIndex < 0 ? (win.choicesError || (win.choices.length ? qsTr("Choose a PR branch") : qsTr("No open PR found"))) : currentText
                                        onActivated: index => send("select", card.p.id, {key: win.choices[index].key})
                                    }
                                    Button { text: qsTr("Set upstream"); visible: !card.p.upstream; onClicked: { upstreamField.text = card.p.candidate; upstreamError.text = ""; upstreamDialog.open() } }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Button { text: card.p.tracked ? qsTr("Change branch") : qsTr("Track branch"); onClicked: win.track(card.p) }
                                    Button { visible: !!card.p.tracked; text: qsTr("Use PRs"); onClicked: send("clear_track", card.p.id) }
                                    Item { Layout.fillWidth: true }
                                    Button { text: qsTr("Sync now"); highlighted: true; enabled: !backend.busy; onClicked: send("sync", card.p.id) }
                                }
                                RowLayout {
                                    visible: card.last.ok === false; Layout.fillWidth: true
                                    Label { text: card.last.files && card.last.files.length ? "Files: " + card.last.files.join(", ") : "See error details"; opacity: 0.7; Layout.fillWidth: true; elide: Text.ElideRight }
                                    Button { text: qsTr("Copy error"); onClicked: send("copy", card.p.id) }
                                    Button { text: qsTr("Ask agent"); onClicked: send("agent", card.p.id) }
                                }
                            }
                        }
                    }
                }
                Label { visible: backend.plugins.length === 0 && !backend.busy; text: qsTr("No plugins found"); opacity: 0.6; Layout.topMargin: 32 }
                Label { visible: backend.plugins.length > 0 && !win.showOther && backend.plugins.filter(p => p.attention).length === 0 && !backend.busy; text: qsTr("No plugins need attention. Turn on Other plugins to see them all."); opacity: 0.6; Layout.topMargin: 32 }
            }
        }
    }
    Dialog {
        id: trackDialog; title: qsTr("Track a GitHub branch"); modal: true; anchors.centerIn: parent; width: Math.min(win.width - 48, 560)
        standardButtons: Dialog.Cancel
        ColumnLayout {
            width: parent.width; spacing: 8
            Label { text: qsTr("Choose the upstream and your GitHub branch."); wrapMode: Text.Wrap; Layout.fillWidth: true }
            TextField { id: trackUpstream; placeholderText: qsTr("Upstream repository (owner/name)"); Layout.fillWidth: true; onTextChanged: win.suggestFork() }
            TextField { id: trackBase; placeholderText: qsTr("Upstream branch"); Layout.fillWidth: true }
            TextField { id: trackRepo; placeholderText: qsTr("Your GitHub repository (owner/name)"); Layout.fillWidth: true }
            TextField { id: trackHead; placeholderText: qsTr("Your GitHub branch"); Layout.fillWidth: true }
            Label { id: trackError; color: "#ff8585"; wrapMode: Text.Wrap; Layout.fillWidth: true; visible: text.length > 0 }
            Button { text: qsTr("Track branch"); highlighted: true; Layout.alignment: Qt.AlignRight; onClicked: send("track", win.expandedId, {upstream: trackUpstream.text, base: trackBase.text, head_repo: trackRepo.text, head: trackHead.text}) }
        }
    }
    Dialog {
        id: upstreamDialog; title: qsTr("Set upstream repository"); modal: true; anchors.centerIn: parent; width: Math.min(win.width - 48, 560); standardButtons: Dialog.Cancel
        ColumnLayout {
            width: parent.width
            TextField { id: upstreamField; placeholderText: qsTr("GitHub repository URL"); Layout.fillWidth: true }
            Label { id: upstreamError; color: "#ff8585"; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Button { text: qsTr("Set upstream"); highlighted: true; Layout.alignment: Qt.AlignRight; onClicked: send("upstream", win.expandedId, {url: upstreamField.text}) }
        }
    }
    Dialog {
        id: hookDialog; title: qsTr("Set up automatic sync?"); modal: true; anchors.centerIn: parent
        standardButtons: Dialog.Cancel | Dialog.Ok
        onAccepted: send("hook")
        Label { text: qsTr("Install the Omarchy post-update hook to sync enabled plugins after updates."); wrapMode: Text.Wrap; width: 380 }
    }
    Dialog {
        id: helpDialog; title: qsTr("Keyboard shortcuts"); modal: true; anchors.centerIn: parent; standardButtons: Dialog.Close
        Label { text: qsTr("?    Show shortcuts\nQ    Quit\nCtrl+R    Refresh") }
    }
}
