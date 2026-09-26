import QtQuick
import QtQuick.Templates as T

// The app's tooltip: declare one inside a control instead of the attached
// ToolTip.text/visible, e.g. Tip { text: "..."; visible: parent.hovered }.
// The attached one takes the platform style's look - faint on macOS - and
// shows the moment the mouse passes, over whatever sits above the control.
// This one is built on the bare template so no style can fade it, waits
// before showing, opens below the control and wraps long help texts.
T.ToolTip {
    id: tip
    delay: 600
    timeout: 10000
    margins: 8
    horizontalPadding: 10
    verticalPadding: 7
    x: parent ? Math.round((parent.width - width) / 2) : 0
    y: parent ? parent.height + 6 : 0
    width: Math.min(360, label.implicitWidth + leftPadding + rightPadding)
    implicitHeight: label.implicitHeight + topPadding + bottomPadding
    closePolicy: T.Popup.CloseOnEscape | T.Popup.CloseOnPressOutsideParent | T.Popup.CloseOnReleaseOutsideParent

    contentItem: Text {
        id: label
        text: tip.text
        color: Theme.tipText
        font.pixelSize: 12
        wrapMode: Text.Wrap
        lineHeight: 1.15
    }
    background: Rectangle {
        color: Theme.tipBg
        border.color: Theme.tipBorder
        radius: 6
    }
}
