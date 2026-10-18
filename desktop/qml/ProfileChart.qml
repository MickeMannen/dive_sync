import QtQuick
import QtQuick.Layouts

// The depth profile of one dive: a heading row and a Canvas drawing the
// depth line over time. Used by the dives pages (DivesPage.qml) and the
// Convert page (ConvertPage.qml); shows nothing for fewer than two samples.
// ``samples`` is a list of {time (s), depth (m), ...} objects, the shape
// dive_cache.get_samples and ConvertController.selected.samples share.
// Temperature is deliberately not plotted: the services record it as whole
// degrees, so the curve is a staircase of 1° steps that reads as noise next
// to the depth line.
// The samples stay metres; the depth axis is labelled in the unit system
// chosen on the Settings page (plans/units.md J4) through ``depthScale``,
// which the pages bind to ``units.scales.depth`` ({factor, label, decimals}
// from src/core/units.py). The default is metres, exactly as before.
ColumnLayout {
    id: chart
    property var samples: []
    property int chartHeight: 160
    property var depthScale: ({ factor: 1, label: "m", decimals: 1 })
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
    // the axis labels, in the chosen unit ("0 m" / "12.5 m", "0 ft" / "41 ft")
    readonly property string depthAxisTop: "0 " + depthScale.label
    readonly property string depthAxisBottom: (maxDepth * depthScale.factor).toFixed(depthScale.decimals) + " " + depthScale.label
    spacing: 4
    visible: hasProfile
    onSamplesChanged: canvas.requestPaint()
    onDepthScaleChanged: canvas.requestPaint()

    RowLayout {
        spacing: 12
        Text { text: "Depth profile"; color: Theme.muted; font.pixelSize: 11 }
        Text { text: "● depth"; color: Theme.accent; font.pixelSize: 11 }
    }
    Canvas {
        id: canvas
        Layout.fillWidth: true
        Layout.preferredHeight: chart.chartHeight
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

            // Right padding is small: nothing is labelled on that edge.
            var pad = { left: 40, right: 12, top: 10, bottom: 20 }
            var plotW = width - pad.left - pad.right
            var plotH = height - pad.top - pad.bottom
            if (plotW <= 0 || plotH <= 0) return

            var maxTime = samples[samples.length - 1].time || 0
            var maxDepth = chart.maxDepth
            if (maxTime <= 0) maxTime = 1

            function xAt(t) { return pad.left + (t / maxTime) * plotW }
            function yDepth(d) { return pad.top + (d / maxDepth) * plotH }

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
        }
    }
}
