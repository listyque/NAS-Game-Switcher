.pragma library

// Prefer controls overlapping the direction's axis, then the nearest visible edge.
function next(items, current, action, space) {
    const dx = action === "right" ? 1 : action === "left" ? -1 : 0
    const dy = action === "down" ? 1 : action === "up" ? -1 : 0
    const selected = items[current]
    if (!selected) return current
    const a = selected.mapToItem(space, 0, 0)
    let best = current, score = Infinity
    for (let i = 0; i < items.length; i++) {
        const item = items[i]
        if (!item || i === current || !item.visible || !item.enabled) continue
        const b = item.mapToItem(space, 0, 0)
        const x = b.x + item.width / 2 - a.x - selected.width / 2
        const y = b.y + item.height / 2 - a.y - selected.height / 2
        const along = x * dx + y * dy
        if (along <= 1) continue
        const across = Math.abs(x * dy - y * dx)
        const overlap = dx ? Math.min(a.y + selected.height, b.y + item.height) - Math.max(a.y, b.y)
                           : Math.min(a.x + selected.width, b.x + item.width) - Math.max(a.x, b.x)
        if (dx && overlap <= 1) continue
        const edge = dx > 0 ? b.x - a.x - selected.width : dx < 0 ? a.x - b.x - item.width
                   : dy > 0 ? b.y - a.y - selected.height : a.y - b.y - item.height
        const distance = (overlap > 1 ? 0 : 100000) + Math.max(0, edge) + along * .01 + across * .001
        if (distance < score) { score = distance; best = i }
    }
    return best
}
