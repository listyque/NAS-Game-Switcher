import QtQuick

Canvas {
    required property string kind
    required property var state
    readonly property var themeColors: uiTheme.themeColors
    onThemeColorsChanged: requestPaint()
    onKindChanged: requestPaint()
    onStateChanged: requestPaint()
    onPaint: {
        const c = getContext("2d")
        c.reset()
        c.scale(width / 24, height / 24)
        const radio = kind === "wifi" || kind === "bluetooth"
        const disabled = radio && state[kind] !== "on"
        c.strokeStyle = disabled ? uiTheme.themeColors.muted : uiTheme.themeColors.text
        c.fillStyle = c.strokeStyle
        c.lineWidth = 1.7; c.lineCap = "round"; c.lineJoin = "round"
        c.beginPath()
        if (kind === "wifi") {
            for (let r of [5, 9, 13]) { c.moveTo(12 - r * .707, 18 - r * .707); c.arc(12, 18, r, Math.PI * 1.25, Math.PI * 1.75) }
            c.stroke(); c.beginPath(); c.arc(12, 19, 1.4, 0, Math.PI * 2); c.fill()
        } else if (kind === "bluetooth") {
            c.moveTo(7, 7); c.lineTo(17, 17); c.lineTo(12, 22); c.lineTo(12, 2); c.lineTo(17, 7); c.lineTo(7, 17); c.stroke()
        } else if (kind === "battery") {
            c.rect(2, 6, 18, 12); c.stroke(); c.fillRect(21, 10, 2, 4)
            if (state.has_battery && state.battery >= 0) {
                c.fillStyle = state.battery <= 20 && !state.plugged ? "#ffac83" : uiTheme.themeColors.text
                c.fillRect(4, 8, 14 * state.battery / 100, 8)
            }
            if (state.charging) { c.strokeStyle = uiTheme.themeColors.accent; c.beginPath(); c.moveTo(14, 2); c.lineTo(9, 12); c.lineTo(15, 12); c.lineTo(10, 22); c.stroke() }
        } else if (kind === "brightness") {
            c.arc(12, 12, 4, 0, Math.PI * 2); c.stroke()
            for (let i = 0; i < 8; i++) {
                const angle = i * Math.PI / 4
                c.beginPath(); c.moveTo(12 + Math.cos(angle) * 7, 12 + Math.sin(angle) * 7)
                c.lineTo(12 + Math.cos(angle) * 10, 12 + Math.sin(angle) * 10); c.stroke()
            }
        } else if (kind === "volume") {
            c.moveTo(3, 9); c.lineTo(7, 9); c.lineTo(12, 5); c.lineTo(12, 19); c.lineTo(7, 15); c.lineTo(3, 15); c.closePath(); c.stroke()
            if (state.muted) { c.beginPath(); c.moveTo(16, 9); c.lineTo(22, 15); c.moveTo(22, 9); c.lineTo(16, 15); c.stroke() }
            else if (state.volume >= 0) {
                for (let r of (state.volume > 50 ? [5, 9] : state.volume > 0 ? [5] : [])) { c.beginPath(); c.arc(12, 12, r, -Math.PI / 4, Math.PI / 4); c.stroke() }
            }
        }
        if (disabled) { c.beginPath(); c.moveTo(3, 22); c.lineTo(22, 3); c.stroke() }
        if ((radio && (state[kind] === "unknown" || state[kind] === "absent")) || (kind === "volume" && state.volume < 0 && !state.muted)) {
            c.fillStyle = uiTheme.themeColors.text; c.font = "bold 10px sans-serif"; c.fillText(state[kind] === "absent" ? "−" : "?", 18, 23)
        }
    }
}
