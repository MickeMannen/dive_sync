import QtQuick
import QtQuick.Layouts

// The depth profile of one dive: a heading row and a Canvas drawing the
// depth line over time. Used by the dives pages (DivesPage.qml) and the
// Dive edit page (ConvertPage.qml); shows nothing for fewer than two samples.
// ``samples`` is a list of {time (s), depth (m), ...} objects, the shape
// dive_cache.get_samples and ConvertController.selected.samples share.
// Temperature is deliberately not plotted: the services record it as whole
// degrees, so the curve is a staircase of 1° steps that reads as noise next
// to the depth line.
// The samples stay metres; the depth axis is labelled in the unit system
// chosen on the Settings page (plans/units.md J4) through ``depthScale``,
// which the pages bind to ``units.scales.depth`` ({factor, label, decimals}
// from src/core/units.py). The default is metres, exactly as before.
//
// Hover (plans/convert.md I19): the sample nearest the mouse gets a vertical
// line and a dot, and a tooltip with the lines ``detailsProvider(index)``
// returns (a controller slot, src/core/convert/profile.sample_details: the
// chart never holds per-sample strings). A click pins the tooltip at that
// sample (a diamond at the top of the line; click again or Esc unpins; the
// mouse moves nothing while pinned), and with the chart focused (a click
// gives focus) Left/Right step one sample, Shift+arrow ten, pinning as they
// go. ``markers`` ([{time, label}], the gas switches) draw a tick with the
// label above the profile; ``ceiling`` ([{time, depth}] metres, the deco
// stop) a translucent band from the surface down to it, under the line.
// Without a provider the tooltip shows the time and depth from the sample
// itself. With nothing hovered and no markers or ceiling the drawing is
// exactly what it was before I19.
ColumnLayout {
    id: chart
    property var samples: []
    property int chartHeight: 160
    property var depthScale: ({ factor: 1, label: "m", decimals: 1 })
    // I19: function (index) -> {lines: [...], ...} for one sample; null = the chart's own time/depth
    property var detailsProvider: null
    property var markers: []
    property var ceiling: []
    // the sample under the mouse / the pinned one; activeIndex is what is drawn
    property int hoverIndex: -1
    property int pinnedIndex: -1
    readonly property bool pinned: pinnedIndex >= 0
    readonly property int activeIndex: pinned ? pinnedIndex : hoverIndex
    // the details of activeIndex, as the provider gave them ({} for none)
    property var details: ({})
    readonly property var tooltipLines: (details && details.lines) ? details.lines : []
    // two samples make a line; with fewer the whole component hides
    readonly property bool hasProfile: (samples || []).length > 1
    // the deepest sample in metres (1 for a flat profile, so the scale holds)
    readonly property real maxDepth: {
        var list = samples || []
        var deepest = 0
        for (var i = 0; i < list.length; i++) {
            if (list[i].depth > deepest) deepest = list[i].depth
        }
        return deepest > 0 ? deepest : 1
    }
    // the last sample's time (1 for a profile without times, so the scale holds)
    readonly property real maxTime: {
        var list = samples || []
        var t = list.length > 0 ? (list[list.length - 1].time || 0) : 0
        return t > 0 ? t : 1
    }
    // the axis labels, in the chosen unit ("0 m" / "12.5 m", "0 ft" / "41 ft")
    readonly property string depthAxisTop: "0 " + depthScale.label
    readonly property string depthAxisBottom: (maxDepth * depthScale.factor).toFixed(depthScale.decimals) + " " + depthScale.label
    // the plot's padding inside the canvas; right is small: nothing is labelled on that edge
    readonly property int padLeft: 40
    readonly property int padRight: 12
    readonly property int padTop: 10
    readonly property int padBottom: 20
    readonly property real plotW: plot.width - padLeft - padRight
    readonly property real plotH: plot.height - padTop - padBottom
    spacing: 4
    visible: hasProfile
    activeFocusOnTab: true
    onSamplesChanged: { hoverIndex = -1; pinnedIndex = -1; refreshDetails(); canvas.requestPaint() }
    onDepthScaleChanged: { refreshDetails(); canvas.requestPaint() }
    onMarkersChanged: canvas.requestPaint()
    onCeilingChanged: canvas.requestPaint()
    onActiveIndexChanged: refreshDetails()
    onDetailsProviderChanged: refreshDetails()

    function xAt(t) { return padLeft + (t / maxTime) * plotW }
    function yAt(d) { return padTop + (d / maxDepth) * plotH }
    function timeOf(i) { return samples[i].time || 0 }
    // the sample nearest to a canvas x (binary search on the times), -1 for no profile
    function indexAtX(x) {
        var list = samples || []
        if (list.length === 0 || plotW <= 0) return -1
        var t = (x - padLeft) / plotW * maxTime
        var lo = 0, hi = list.length - 1
        while (lo < hi) {
            var mid = (lo + hi) >> 1
            if ((list[mid].time || 0) < t) lo = mid + 1; else hi = mid
        }
        if (lo > 0 && t - (list[lo - 1].time || 0) < (list[lo].time || 0) - t) lo -= 1
        return lo
    }
    function refreshDetails() {
        var i = activeIndex
        if (i < 0 || i >= (samples || []).length) { details = {}; return }
        details = detailsProvider ? (detailsProvider(i) || {}) : localDetails(i)
    }
    // time and depth from the sample itself, for a chart without a provider
    function localDetails(i) {
        var t = timeOf(i)
        var pad2 = function (n) { return (n < 10 ? "0" : "") + n }
        var clock = t >= 3600 ? Math.floor(t / 3600) + ":" + pad2(Math.floor(t % 3600 / 60)) + ":" + pad2(t % 60)
                              : Math.floor(t / 60) + ":" + pad2(t % 60)
        var depth = (samples[i].depth * depthScale.factor).toFixed(depthScale.decimals) + " " + depthScale.label
        return { time: clock, depth: depth, lines: [clock, depth] }
    }
    // the mouse is followed even while pinned, so unpinning lands on the sample under it
    function hoverAt(x) { hoverIndex = indexAtX(x) }
    function pin(i) { pinnedIndex = i }
    function unpin() { pinnedIndex = -1 }
    function togglePin(i) { if (pinned && pinnedIndex === i) unpin(); else pin(i) }
    // the arrow keys: from the active sample (or the start), clamped, pinning
    function step(delta) {
        var n = (samples || []).length
        if (n === 0) return
        var base = activeIndex >= 0 ? activeIndex : (delta < 0 ? n : -1)
        pin(Math.max(0, Math.min(n - 1, base + delta)))
    }
    Keys.onLeftPressed: function (event) { step(event.modifiers & Qt.ShiftModifier ? -10 : -1); event.accepted = true }
    Keys.onRightPressed: function (event) { step(event.modifiers & Qt.ShiftModifier ? 10 : 1); event.accepted = true }
    Keys.onEscapePressed: function (event) { if (pinned) { unpin(); event.accepted = true } }

    RowLayout {
        spacing: 12
        Text { text: "Depth profile"; color: Theme.muted; font.pixelSize: 11 }
        Text { text: "● depth"; color: Theme.accent; font.pixelSize: 11 }
        Text {
            visible: chart.activeIndex >= 0
            text: chart.pinned ? "pinned · click or Esc unpins · ← → step" : "click pins · ← → step"
            color: Theme.muted; font.pixelSize: 11
        }
    }
    Item {
        id: plot
        objectName: "profilePlot"
        Layout.fillWidth: true
        Layout.preferredHeight: chart.chartHeight

        Canvas {
            id: canvas
            anchors.fill: parent
            // drawn once into a texture off the GUI thread, then only moved
            // while the page scrolls
            renderTarget: Canvas.FramebufferObject
            renderStrategy: Canvas.Cooperative
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                var samples = chart.samples || []
                if (samples.length < 2 || width <= 0 || height <= 0) return

                var pad = { left: chart.padLeft, right: chart.padRight, top: chart.padTop, bottom: chart.padBottom }
                var plotW = width - pad.left - pad.right
                var plotH = height - pad.top - pad.bottom
                if (plotW <= 0 || plotH <= 0) return

                var maxTime = samples[samples.length - 1].time || 0
                var maxDepth = chart.maxDepth
                if (maxTime <= 0) maxTime = 1

                function xAt(t) { return pad.left + (t / maxTime) * plotW }
                function yDepth(d) { return pad.top + (d / maxDepth) * plotH }

                // the deco ceiling band, under everything else (I19)
                var ceiling = chart.ceiling || []
                if (ceiling.length > 0) {
                    ctx.fillStyle = Qt.alpha(Theme.danger, 0.16)
                    ctx.beginPath()
                    ctx.moveTo(xAt(ceiling[0].time || 0), pad.top)
                    for (var c = 0; c < ceiling.length; c++) ctx.lineTo(xAt(ceiling[c].time || 0), yDepth(ceiling[c].depth || 0))
                    ctx.lineTo(xAt(ceiling[ceiling.length - 1].time || 0), pad.top)
                    ctx.closePath()
                    ctx.fill()
                }

                ctx.strokeStyle = Theme.border
                ctx.lineWidth = 1
                ctx.beginPath()
                ctx.moveTo(pad.left, pad.top)
                ctx.lineTo(pad.left, pad.top + plotH)
                ctx.lineTo(pad.left + plotW, pad.top + plotH)
                ctx.stroke()

                ctx.strokeStyle = Theme.accent
                ctx.lineWidth = 1.5
                ctx.beginPath()
                for (var j = 0; j < samples.length; j++) {
                    var x = xAt(samples[j].time || 0)
                    var y = yDepth(samples[j].depth)
                    if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                }
                ctx.stroke()

                ctx.fillStyle = Theme.muted
                ctx.font = "10px sans-serif"
                ctx.fillText(chart.depthAxisTop, 4, pad.top + 8)
                ctx.fillText(chart.depthAxisBottom, 4, pad.top + plotH)
                ctx.fillText(Math.round(maxTime / 60) + " min", pad.left + plotW - 26, height - 4)

                // the gas switches: a tick above the line with the gas name over it (I19)
                var markers = chart.markers || []
                if (markers.length > 0) {
                    ctx.strokeStyle = Theme.text
                    ctx.fillStyle = Theme.text
                    ctx.lineWidth = 1
                    ctx.textAlign = "center"
                    for (var m = 0; m < markers.length; m++) {
                        var mt = markers[m].time || 0
                        var mi = chart.indexAtX(xAt(mt))
                        var mx = xAt(mt)
                        var my = yDepth(mi >= 0 ? samples[mi].depth : 0)
                        ctx.beginPath()
                        ctx.moveTo(mx, my - 3)
                        ctx.lineTo(mx, my - 11)
                        ctx.stroke()
                        ctx.fillText(markers[m].label || "", mx, my - 13)
                    }
                    ctx.textAlign = "left"
                }
            }
        }
        MouseArea {
            id: hover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton
            onPositionChanged: function (mouse) { chart.hoverAt(mouse.x) }
            onExited: chart.hoverIndex = -1
            onClicked: function (mouse) {
                chart.forceActiveFocus()
                var i = chart.pinned ? chart.pinnedIndex : chart.indexAtX(mouse.x)
                if (i >= 0) chart.togglePin(i)
            }
        }
        // the cursor: a vertical line through the plot, a dot on the sample, a diamond when pinned
        readonly property bool showCursor: chart.activeIndex >= 0 && chart.activeIndex < (chart.samples || []).length
        readonly property real cursorX: showCursor ? chart.xAt(chart.timeOf(chart.activeIndex)) : 0
        readonly property real cursorY: showCursor ? chart.yAt(chart.samples[chart.activeIndex].depth) : 0
        Rectangle {
            objectName: "cursorLine"
            visible: plot.showCursor
            x: plot.cursorX; y: chart.padTop; width: 1; height: chart.plotH
            color: Theme.muted
        }
        Rectangle {
            objectName: "cursorDot"
            visible: plot.showCursor
            width: 7; height: 7; radius: 3.5
            x: plot.cursorX - 3.5; y: plot.cursorY - 3.5
            color: Theme.accent; border.color: Theme.card; border.width: 1
        }
        Rectangle {
            objectName: "pinMark"
            visible: plot.showCursor && chart.pinned
            width: 7; height: 7; rotation: 45
            x: plot.cursorX - 3.5; y: chart.padTop - 3.5
            color: Theme.accent
        }
        // the tooltip, beside the cursor: right of it, or left of it near the right edge, inside the plot
        Rectangle {
            id: tip
            objectName: "sampleTip"
            visible: plot.showCursor && chart.tooltipLines.length > 0
            width: tipColumn.implicitWidth + 12
            height: tipColumn.implicitHeight + 8
            x: plot.cursorX + 10 + width > plot.width ? Math.max(0, plot.cursorX - 10 - width) : plot.cursorX + 10
            y: Math.max(0, Math.min(plot.cursorY - height / 2, plot.height - height))
            color: Theme.tipBg
            border.color: Theme.tipBorder
            radius: 4
            Column {
                id: tipColumn
                x: 6; y: 4
                spacing: 1
                Repeater {
                    model: chart.tooltipLines
                    Text { text: modelData; color: Theme.tipText; font.pixelSize: 11 }
                }
            }
        }
    }
}
