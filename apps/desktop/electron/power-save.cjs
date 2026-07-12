const fs = require('node:fs')

const DEFAULT_SURFACES = ['desktop', 'gateway']
const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on'])
const FALSE_VALUES = new Set(['0', 'false', 'no', 'off'])
const VALID_MODES = new Set(['system', 'display'])

function normalizeSurface(value) {
  return String(value || '').trim().toLowerCase().replace(/_/g, '-')
}

function parseBool(value, fallback = undefined) {
  if (typeof value === 'boolean') return value
  if (value === undefined || value === null) return fallback
  const text = String(value).trim().toLowerCase()
  if (TRUE_VALUES.has(text)) return true
  if (FALSE_VALUES.has(text)) return false
  return fallback
}

function normalizeSurfaces(value) {
  if (value === undefined || value === null || value === '') return [...DEFAULT_SURFACES]
  let items
  if (Array.isArray(value)) {
    items = value
  } else {
    const raw = String(value).trim().replace(/^\[/, '').replace(/\]$/, '')
    items = raw.includes(',') ? raw.split(',') : raw.split(/\s+/)
  }
  const surfaces = []
  for (const item of items) {
    const normalized = normalizeSurface(String(item).trim().replace(/^['"]|['"]$/g, ''))
    if (normalized && !surfaces.includes(normalized)) surfaces.push(normalized)
  }
  return surfaces.length ? surfaces : [...DEFAULT_SURFACES]
}

function parseScalar(value) {
  const trimmed = String(value || '').trim()
  const bool = parseBool(trimmed, undefined)
  if (bool !== undefined) return bool
  if (trimmed.startsWith('[') && trimmed.endsWith(']')) return normalizeSurfaces(trimmed)
  return trimmed.replace(/^['"]|['"]$/g, '')
}

function leadingSpaces(line) {
  return (line.match(/^\s*/) || [''])[0].length
}

function readPreventSleepConfigFromYaml(configPath) {
  if (!configPath) return {}
  let text = ''
  try {
    text = fs.readFileSync(configPath, 'utf8')
  } catch {
    return {}
  }

  const lines = text.split(/\r?\n/)
  const result = {}
  let inPower = false
  let powerIndent = -1
  let inPreventSleep = false
  let preventIndent = -1
  let pendingListKey = null
  let pendingListIndent = -1

  for (const rawLine of lines) {
    const line = rawLine.replace(/#.*$/, '')
    if (!line.trim()) continue
    const indent = leadingSpaces(line)
    const trimmed = line.trim()

    if (pendingListKey && indent > pendingListIndent && trimmed.startsWith('- ')) {
      const item = trimmed.slice(2).trim()
      ;(result[pendingListKey] ||= []).push(parseScalar(item))
      continue
    }
    pendingListKey = null

    if (!inPower) {
      if (indent === 0 && trimmed === 'power:') {
        inPower = true
        powerIndent = indent
      }
      continue
    }
    if (indent <= powerIndent && trimmed !== 'power:') {
      inPower = false
      inPreventSleep = false
      continue
    }
    if (!inPreventSleep) {
      if (indent > powerIndent && trimmed === 'prevent_sleep:') {
        inPreventSleep = true
        preventIndent = indent
      }
      continue
    }
    if (indent <= preventIndent) {
      inPreventSleep = false
      continue
    }

    const match = trimmed.match(/^([A-Za-z0-9_-]+):(?:\s*(.*))?$/)
    if (!match) continue
    const key = match[1]
    const value = match[2] || ''
    if (!value) {
      result[key] = []
      pendingListKey = key
      pendingListIndent = indent
    } else {
      result[key] = parseScalar(value)
    }
  }

  return result
}

function normalizeConfigBlock(block) {
  if (typeof block === 'boolean') {
    return { enabled: block, surfaces: [...DEFAULT_SURFACES], mode: 'system' }
  }
  const raw = block && typeof block === 'object' ? block : {}
  const mode = VALID_MODES.has(String(raw.mode || '').trim().toLowerCase())
    ? String(raw.mode).trim().toLowerCase()
    : 'system'
  return {
    enabled: Boolean(parseBool(raw.enabled, false)),
    surfaces: normalizeSurfaces(raw.surfaces),
    mode
  }
}

function resolvePreventSleepConfig({ surface = 'desktop', configPath, block } = {}) {
  const normalizedSurface = normalizeSurface(surface)
  const fileBlock = block === undefined ? readPreventSleepConfigFromYaml(configPath) : block
  const config = normalizeConfigBlock(fileBlock)

  return {
    enabled: Boolean(config.enabled && config.surfaces.includes(normalizedSurface)),
    mode: config.mode,
    surfaces: config.surfaces
  }
}

function powerSaveTypeForMode(mode) {
  return mode === 'display' ? 'prevent-display-sleep' : 'prevent-app-suspension'
}

function createPowerSaveBlockerController(powerSaveBlocker, options = {}) {
  let blockerId = null
  const config = resolvePreventSleepConfig(options)

  return {
    config,
    start() {
      if (!config.enabled || !powerSaveBlocker || typeof powerSaveBlocker.start !== 'function') {
        return false
      }
      if (blockerId !== null) return true
      blockerId = powerSaveBlocker.start(powerSaveTypeForMode(config.mode))
      return true
    },
    stop() {
      if (blockerId === null || !powerSaveBlocker || typeof powerSaveBlocker.stop !== 'function') {
        return false
      }
      const id = blockerId
      blockerId = null
      powerSaveBlocker.stop(id)
      return true
    },
    isStarted() {
      if (blockerId === null) return false
      if (!powerSaveBlocker || typeof powerSaveBlocker.isStarted !== 'function') return true
      return powerSaveBlocker.isStarted(blockerId)
    },
    id() {
      return blockerId
    }
  }
}

module.exports = {
  createPowerSaveBlockerController,
  normalizeSurfaces,
  powerSaveTypeForMode,
  readPreventSleepConfigFromYaml,
  resolvePreventSleepConfig
}
