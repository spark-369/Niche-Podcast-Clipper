/**
 * CaptionOverlay - draws the clip's captions on a canvas over the preview video.
 *
 * The editor used to fetch a server-composited JPEG for every ~0.5s of
 * playback. Each request meant a video seek, a full crop/caption composite and
 * a JPEG encode, and they arrived slower than they were requested, so the
 * overlay fell further behind the audio on every loop until it looked frozen —
 * worst on short 9:16 frames, where proportionally more of the picture is
 * caption.
 *
 * Instead the whole caption timeline is fetched once (see
 * `GET /clips/{id}/caption-timeline`) and redrawn here on every animation
 * frame, reading `video.currentTime` directly. Captions now change on the exact
 * frame the word changes, with no network traffic during playback.
 *
 * This canvas is the **only** caption track in the editor preview. The preview
 * plays the episode's source media, which has no captions, because playing the
 * rendered MP4 showed its burned-in captions underneath these draft ones — two
 * caption tracks as soon as the draft style differed from the stored render.
 *
 * `timeOffset` is the clip's in-point. The source media is the whole episode,
 * so `video.currentTime` is absolute while the caption timeline is
 * clip-relative; the offset rebases one onto the other.
 */

import { useEffect, useRef } from 'react'

// Cap the backing canvas so a large phone-frame does not force the browser to
// rasterise millions of pixels every animation frame.
const MAX_CANVAS_WIDTH = 540
const MAX_CANVAS_HEIGHT = 960

/** Size a canvas' backing store to its rendered box, capped for performance. */
function syncCanvasSize(canvas) {
  const rect = canvas.getBoundingClientRect()
  if (!rect.width || !rect.height) return false
  const ratio = Math.min(MAX_CANVAS_WIDTH / rect.width, MAX_CANVAS_HEIGHT / rect.height, 2)
  const nextWidth = Math.max(Math.round(rect.width * ratio), 1)
  const nextHeight = Math.max(Math.round(rect.height * ratio), 1)
  if (canvas.width === nextWidth && canvas.height === nextHeight) return false
  canvas.width = nextWidth
  canvas.height = nextHeight
  return true
}

// Mirrors `services/video/captions.py`.
const LINE_SPACING = 1.18
const FONT_SCALE_DIVISOR = 32.0

function hexToRgb(value, fallback = [255, 255, 255]) {
  if (!value || typeof value !== 'string') return fallback
  let raw = value.trim().replace(/^#/, '')
  if (raw.length === 3) {
    raw = raw
      .split('')
      .map((char) => char + char)
      .join('')
  }
  if (raw.length === 8) raw = raw.slice(0, 6)
  if (raw.length !== 6) return fallback
  const parsed = Number.parseInt(raw, 16)
  if (Number.isNaN(parsed)) return fallback
  return [(parsed >> 16) & 255, (parsed >> 8) & 255, parsed & 255]
}

function toRgba(rgb, alpha = 1) {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`
}

/** The group whose window contains `time`, or null. Mirrors `_active_group`. */
function activeGroup(groups, time) {
  if (!groups || groups.length === 0) return null
  for (const group of groups) {
    if (!group.words || group.words.length === 0) continue
    if (group.start <= time && time <= group.end) return group
  }
  return null
}

function easeOut(progress) {
  const clamped = Math.max(0, Math.min(progress, 1))
  return 1 - Math.pow(1 - clamped, 3)
}

/** Draw the group's visible lines, centred, with the active word highlighted. */
function drawGroup(context, { group, activeIndex, progress, width, height, style, timeline }) {
  const wordsPerLine = Math.max(Number(timeline?.words_per_line) || 3, 1)
  const maxLines = Math.max(Number(timeline?.max_lines) || 2, 1)

  const lines = []
  for (let index = 0; index < group.words.length; index += wordsPerLine) {
    lines.push(group.words.slice(index, index + wordsPerLine))
    if (lines.length >= maxLines) break
  }
  if (lines.length === 0) return

  // Font sizes are authored against a 1920px-tall canvas; scale to this frame
  // so captions look the same on any output size. Mirrors `draw_captions`.
  const heightFactor = height / 1920
  const fontFactor = Math.max(heightFactor, 0.45)
  const widthFactor = width / 1080
  const baseFontSize =
    (Number(style?.font_size) || 64) * Math.min(fontFactor, Math.max(widthFactor, 0.45))

  const fontFamily = style?.font_family || 'sans-serif'
  const uppercase = style?.uppercase !== false
  const outlineWidth = Math.max(Number(style?.outline_width) || 0, 0)
  const pop = Number(style?.pop_scale) || 1.15
  const textColor = hexToRgb(style?.text_color, [255, 255, 255])
  const highlightColor = hexToRgb(style?.highlight_color, [255, 45, 85])
  const outlineColor = hexToRgb(style?.outline_color, [0, 0, 0])

  const lineHeight = baseFontSize * LINE_SPACING
  const position = style?.position || 'bottom'
  const bottomMargin = Math.min(
    (Number(style?.bottom_margin) || 320) * (height / 1920),
    height * 0.25,
  )

  let firstBaseline
  if (position === 'top') {
    firstBaseline = height * 0.08 + baseFontSize * 1.2
  } else if (position === 'center') {
    firstBaseline = height / 2 - (lineHeight * lines.length) / 2 + baseFontSize
  } else {
    firstBaseline = Math.max(height - bottomMargin, baseFontSize * 2)
  }

  const backgroundRgb = style?.background_color ? hexToRgb(style.background_color, null) : null
  const backgroundOpacity = Number(style?.background_opacity) || 0
  if (backgroundRgb && backgroundOpacity > 0) {
    const blockTop = Math.max(firstBaseline - baseFontSize * 1.3, 0)
    const blockBottom = Math.min(
      firstBaseline + lineHeight * (lines.length - 1) + baseFontSize * 0.6,
      height,
    )
    context.fillStyle = toRgba(backgroundRgb, backgroundOpacity)
    context.fillRect(0, blockTop, width, blockBottom - blockTop)
  }

  context.textBaseline = 'alphabetic'
  context.textAlign = 'left'

  lines.forEach((line, lineIndex) => {
    let localActive = activeIndex - lineIndex * wordsPerLine
    if (localActive < 0 || localActive >= line.length) localActive = -1

    const rendered = line.map((word) =>
      uppercase ? String(word.text || '').toUpperCase() : String(word.text || ''),
    )

    let fontSize = baseFontSize
    context.font = `700 ${fontSize}px ${fontFamily}, sans-serif`
    let widths = rendered.map((text) => context.measureText(text).width)
    let gap = Math.max((fontSize / FONT_SCALE_DIVISOR) * 16, 2)
    let totalWidth =
      widths.reduce((sum, value) => sum + value, 0) + gap * Math.max(rendered.length - 1, 0)

    // Shrink a line that overflows instead of clipping words off the edges.
    const maxLineWidth = width * 0.92
    if (totalWidth > maxLineWidth && totalWidth > 0) {
      const shrink = maxLineWidth / totalWidth
      fontSize *= shrink
      context.font = `700 ${fontSize}px ${fontFamily}, sans-serif`
      widths = rendered.map((text) => context.measureText(text).width)
      gap = Math.max((fontSize / FONT_SCALE_DIVISOR) * 16, 2)
      totalWidth =
        widths.reduce((sum, value) => sum + value, 0) + gap * Math.max(rendered.length - 1, 0)
    }

    let x = Math.max((width - totalWidth) / 2, 12)
    const baseline = firstBaseline + lineIndex * lineHeight
    const strokeWidth = Math.max((fontSize / FONT_SCALE_DIVISOR) * 1.8, 1)

    for (let index = 0; index < rendered.length; index += 1) {
      const isActive = index === localActive
      const wordFontSize = fontSize * (isActive ? 1 + (pop - 1) * easeOut(progress) : 1)
      context.font = `700 ${wordFontSize}px ${fontFamily}, sans-serif`

      if (outlineWidth > 0 && !isActive) {
        context.lineWidth = strokeWidth + outlineWidth
        context.strokeStyle = toRgba(outlineColor, 1)
        context.lineJoin = 'round'
        context.strokeText(rendered[index], x, baseline)
      }
      if (style?.shadow !== false) {
        context.fillStyle = 'rgba(0, 0, 0, 0.85)'
        context.fillText(rendered[index], x + 3, baseline + 3)
      }
      context.fillStyle = toRgba(isActive ? highlightColor : textColor, 1)
      context.fillText(rendered[index], x, baseline)

      x += widths[index] + gap
    }
  })
}

/**
 * Draw the clip's title card over the opening ~1.6s. Mirrors the renderer's
 * `draw_title_card`: uppercase title in the highlight colour on a translucent
 * black box, fading in over the first 0.4s near the top of the frame.
 */
function drawTitleCard(context, { title, clipTime, width, height, style }) {
  const text = String(title || '').trim()
  if (!text) return

  const progress = Math.max(0, Math.min(clipTime / 0.4, 1))
  const heightFactor = Math.max(height / 1920, 0.45)
  const baseFontSize = ((Number(style?.font_size) || 64) * 0.75) * heightFactor
  const fontFamily = style?.font_family || 'sans-serif'
  const highlightColor = hexToRgb(style?.highlight_color, [255, 212, 0])
  const rendered = style?.uppercase !== false ? text.toUpperCase() : text

  let fontSize = baseFontSize
  context.font = `700 ${fontSize}px ${fontFamily}, sans-serif`
  context.textAlign = 'center'
  context.textBaseline = 'alphabetic'
  const maxWidth = width * 0.86
  while (context.measureText(rendered).width > maxWidth && fontSize > 12) {
    fontSize *= 0.97
    context.font = `700 ${fontSize}px ${fontFamily}, sans-serif`
  }
  const x = width / 2
  const y = height * 0.12 + fontSize

  // Translucent black backing box (55% opacity, matching the renderer).
  const boxHeight = fontSize * 1.6
  context.globalAlpha = 0.55 * progress
  context.fillStyle = '#000'
  context.fillRect(0, y - fontSize * 1.2 - 6, width, boxHeight + 12)
  context.globalAlpha = progress
  context.fillStyle = toRgba(highlightColor, 1)
  context.fillText(rendered, x + 3, y + 3)
  context.fillText(rendered, x, y)
  context.globalAlpha = 1
  // Restore left alignment for any later caption drawing in this frame.
  context.textAlign = 'left'
}

export default function CaptionOverlay({
  videoRef,
  timeline,
  style,
  framing,
  title = '',
  timeOffset = 0,
  className,
  // Changes whenever the <video> node is replaced (the editor keys it on the media
  // URL). The ref object itself never changes, so without this the draw loop can
  // keep reading a detached element whose `currentTime` is frozen at 0 — the
  // captions then never match and the overlay stays blank.
  videoKey,
}) {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return undefined
    // Resolved inside the loop below instead of once here: `videoRef.current` can
    // point at a replaced node by the time a frame is drawn.
    if (!videoRef?.current) return undefined

    const context = canvas.getContext('2d')
    if (!context) return undefined

    let frameHandle = 0
    let lastKey = ''

    const draw = () => {
      frameHandle = window.requestAnimationFrame(draw)

      // Resolve the video every frame: the editor replaces the node when the media
      // URL changes, and a captured reference would keep reading the detached one
      // (frozen `currentTime` → no caption ever matches).
      const video = videoRef?.current
      if (!video) return

      // Self-heal the backing store. On the first frames the element may have no
      // layout box yet, which would otherwise leave the canvas at its default
      // 300x150 and draw the captions at the wrong scale forever.
      if (syncCanvasSize(canvas)) lastKey = ''

      const width = canvas.width
      const height = canvas.height
      if (!width || !height) return

      const currentTime = Number.isFinite(video.currentTime) ? video.currentTime : 0
      // `timeOffset` reconciles the timeline's clock with the playing media's. The
      // trimmed companion render is already clip-relative so the editor passes 0;
      // the episode fallback runs on the source timeline and passes the in-point.
      const clipTime = currentTime - (Number(timeOffset) || 0)
      const group = activeGroup(timeline?.groups, clipTime)

      // Which word within the group is active right now.
      let activeWord = null
      let activeIndex = -1
      if (group && group.words) {
        const limit = Math.max(
          (Number(timeline.max_lines) || 2) * (Number(timeline.words_per_line) || 3),
          1,
        )
        const visible = group.words.slice(0, limit)
        for (let index = 0; index < visible.length; index += 1) {
          const word = visible[index]
          if (word.start <= clipTime && clipTime <= word.end) {
            activeWord = word
            activeIndex = index
            break
          }
        }
      }

      const progress = activeWord
        ? Math.max(
            0,
            Math.min(
              (clipTime - activeWord.start) /
                Math.max(activeWord.end - activeWord.start, 0.05),
              1,
            ),
          )
        : 0

      // Skip the repaint when nothing visible changed. requestAnimationFrame
      // fires ~60x/s but the captions change far less often; without this the
      // canvas would be cleared and repainted on every single tick.
      const titleVisible = clipTime <= 1.6
      const key = `${clipTime.toFixed(3)}|${activeIndex}|${progress.toFixed(2)}|${titleVisible ? 't' : ''}`
      if (key === lastKey) return
      lastKey = key

      context.clearRect(0, 0, width, height)

      // The opening title card mirrors the renderer's `draw_title_card`: it shows
      // for the first ~1.6s and fades in over 0.4s regardless of captions, so it
      // is drawn here *before* the caption early-return below.
      if (titleVisible) {
        drawTitleCard(context, { title, clipTime, width, height, style })
      }

      if (!group || activeIndex < 0) return
      // Mirrors the renderer's `framing.get("burn_captions", True)`: the toggle
      // lives on the framing options, not the caption style, so reading it off
      // `style` here would leave the preview showing captions the render omits.
      if (framing?.burn_captions === false) return

      drawGroup(context, { group, activeIndex, progress, width, height, style, timeline })
    }

    frameHandle = window.requestAnimationFrame(draw)
    return () => window.cancelAnimationFrame(frameHandle)
  }, [videoRef, timeline, style, framing, title, timeOffset, videoKey])

  // Keep the backing store in step with the element's box when the layout
  // changes (window resize, aspect-ratio switch). The draw loop also calls this,
  // so a missed resize cannot leave the canvas at the wrong size.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => {
      syncCanvasSize(canvas)
    })
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [])

  return <canvas ref={canvasRef} className={className} aria-hidden="true" />
}
