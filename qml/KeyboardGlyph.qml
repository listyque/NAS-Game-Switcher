import QtQuick

Canvas {
    required property string keyName
    property bool locked: false
    property color ink: "#eeeeef"
    anchors.centerIn: parent
    width: Math.min(28,parent.height*.55); height: width
    onKeyNameChanged: requestPaint()
    onLockedChanged: requestPaint()
    onInkChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    onPaint: {
        const c = getContext("2d"), w = width, h = height
        c.clearRect(0,0,w,h); c.strokeStyle = ink; c.lineWidth = 1.5; c.lineJoin = "round"; c.lineCap = "round"; c.beginPath()
        if (keyName === "Backspace") {
            c.moveTo(w*.3,h*.22); c.lineTo(w*.94,h*.22); c.lineTo(w*.94,h*.78); c.lineTo(w*.3,h*.78); c.lineTo(w*.06,h*.5); c.closePath()
            c.moveTo(w*.47,h*.36); c.lineTo(w*.73,h*.64); c.moveTo(w*.73,h*.36); c.lineTo(w*.47,h*.64)
        } else if (keyName === "Shift" || keyName === "RightShift") {
            c.moveTo(w*.5,h*.08); c.lineTo(w*.91,h*.49); c.lineTo(w*.68,h*.49); c.lineTo(w*.68,h*.9); c.lineTo(w*.32,h*.9); c.lineTo(w*.32,h*.49); c.lineTo(w*.09,h*.49); c.closePath()
            if (locked) { c.fillStyle = c.strokeStyle; c.fill(); c.moveTo(w*.25,h*.99); c.lineTo(w*.75,h*.99) }
        } else if (keyName === "Win") {
            c.rect(w*.16,h*.16,w*.3,h*.3); c.rect(w*.54,h*.16,w*.3,h*.3)
            c.rect(w*.16,h*.54,w*.3,h*.3); c.rect(w*.54,h*.54,w*.3,h*.3)
        } else if (keyName === "Menu") {
            c.rect(w*.2,h*.17,w*.6,h*.66)
            for (let i = 0; i < 3; ++i) { c.moveTo(w*.33,h*(.33+i*.16)); c.lineTo(w*.67,h*(.33+i*.16)) }
        } else if (keyName === "@keyboard") {
            c.rect(w*.08,h*.24,w*.84,h*.56)
            for (let row=0;row<2;++row) for (let col=0;col<5;++col) c.rect(w*(.18+col*.14),h*(.34+row*.14),w*.06,h*.04)
            c.moveTo(w*.32,h*.69); c.lineTo(w*.68,h*.69)
        } else if (keyName === "@overlay") {
            c.rect(w*.12,h*.12,w*.76,h*.76); c.moveTo(w*.12,h*.32); c.lineTo(w*.88,h*.32)
            c.moveTo(w*.5,h*.44); c.lineTo(w*.5,h*.74); c.moveTo(w*.37,h*.61); c.lineTo(w*.5,h*.74); c.lineTo(w*.63,h*.61)
        } else if (keyName === "@settings") {
            for (let i = 0; i < 32; ++i) {
                const angle = i*Math.PI/16-Math.PI/2, radius = i%4 === 1 || i%4 === 2 ? .46 : .35
                const x = w*(.5+radius*Math.cos(angle)), y = h*(.5+radius*Math.sin(angle))
                if (i === 0) c.moveTo(x,y); else c.lineTo(x,y)
            }
            c.closePath(); c.moveTo(w*.66,h*.5); c.arc(w*.5,h*.5,w*.16,0,Math.PI*2)
        } else if (keyName === "@voice") {
            c.moveTo(w*.36,h*.24); c.arc(w*.5,h*.24,w*.14,Math.PI,0); c.lineTo(w*.64,h*.52); c.arc(w*.5,h*.52,w*.14,0,Math.PI); c.closePath()
            c.moveTo(w*.24,h*.47); c.lineTo(w*.24,h*.52); c.arc(w*.5,h*.52,w*.26,Math.PI,0,true); c.lineTo(w*.76,h*.47)
            c.moveTo(w*.5,h*.78); c.lineTo(w*.5,h*.94); c.moveTo(w*.33,h*.94); c.lineTo(w*.67,h*.94)
        } else if (keyName === "@sync") {
            c.arc(w*.5,h*.5,w*.32,-Math.PI*.7,Math.PI*.9)
            c.moveTo(w*.23,h*.17); c.lineTo(w*.23,h*.42); c.lineTo(w*.47,h*.42)
        } else if (keyName === "@power") {
            c.arc(w*.5,h*.53,w*.34,-Math.PI*.33,Math.PI*1.33)
            c.moveTo(w*.5,h*.08); c.lineTo(w*.5,h*.49)
        } else if (keyName === "@play") {
            c.moveTo(w*.3,h*.15); c.lineTo(w*.83,h*.5); c.lineTo(w*.3,h*.85); c.closePath()
        } else if (keyName === "@pause") {
            c.rect(w*.23,h*.15,w*.17,h*.7); c.rect(w*.6,h*.15,w*.17,h*.7)
        } else if (keyName === "@stop") {
            c.rect(w*.2,h*.2,w*.6,h*.6)
        } else if (keyName === "@minimize") {
            c.rect(w*.12,h*.17,w*.76,h*.66); c.moveTo(w*.35,h*.65); c.lineTo(w*.65,h*.65)
        } else if (keyName === "@library") {
            c.rect(w*.12,h*.15,w*.22,h*.7); c.rect(w*.42,h*.15,w*.22,h*.7)
            c.moveTo(w*.72,h*.2); c.lineTo(w*.9,h*.8)
        } else if (keyName === "@memory") {
            c.rect(w*.2,h*.28,w*.6,h*.44)
            for (let i=0;i<4;++i) { const x=w*(.28+i*.15); c.moveTo(x,h*.14); c.lineTo(x,h*.28); c.moveTo(x,h*.72); c.lineTo(x,h*.86) }
        } else if (keyName === "@back") {
            c.moveTo(w*.45,h*.2); c.lineTo(w*.15,h*.5); c.lineTo(w*.45,h*.8); c.moveTo(w*.15,h*.5); c.lineTo(w*.85,h*.5)
        } else { c.moveTo(w*.15,h*.34); c.lineTo(w*.5,h*.69); c.lineTo(w*.85,h*.34) }
        c.stroke()
    }
}
