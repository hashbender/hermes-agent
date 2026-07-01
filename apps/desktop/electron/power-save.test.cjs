const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  createPowerSaveBlockerController,
  powerSaveTypeForMode,
  resolvePreventSleepConfig
} = require('./power-save.cjs')

function writeConfig(body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-power-'))
  const configPath = path.join(dir, 'config.yaml')
  fs.writeFileSync(configPath, body)
  return configPath
}

test('resolvePreventSleepConfig is disabled by default', () => {
  const result = resolvePreventSleepConfig({ surface: 'desktop' })

  assert.equal(result.enabled, false)
  assert.equal(result.mode, 'system')
})

test('config file fallback reads the power.prevent_sleep block', () => {
  const configPath = writeConfig(
    [
      'model: test/model',
      'power:',
      '  prevent_sleep:',
      '    enabled: true',
      '    surfaces:',
      '      - desktop',
      '      - gateway',
      '    mode: system',
      ''
    ].join('\n')
  )

  const result = resolvePreventSleepConfig({ surface: 'gateway', configPath })

  assert.equal(result.enabled, true)
  assert.equal(result.mode, 'system')
  assert.deepEqual(result.surfaces, ['desktop', 'gateway'])
})

test('config can limit the sleep assertion to selected surfaces', () => {
  const configPath = writeConfig(
    [
      'power:',
      '  prevent_sleep:',
      '    enabled: true',
      '    surfaces: [desktop]',
      '    mode: display',
      ''
    ].join('\n')
  )

  assert.equal(resolvePreventSleepConfig({ surface: 'desktop', configPath }).enabled, true)
  assert.equal(resolvePreventSleepConfig({ surface: 'gateway', configPath }).enabled, false)
  assert.equal(resolvePreventSleepConfig({ surface: 'desktop', configPath }).mode, 'display')
})

test('powerSaveTypeForMode keeps display sleep off only when explicitly requested', () => {
  assert.equal(powerSaveTypeForMode('system'), 'prevent-app-suspension')
  assert.equal(powerSaveTypeForMode('display'), 'prevent-display-sleep')
  assert.equal(powerSaveTypeForMode('unknown'), 'prevent-app-suspension')
})

test('controller starts and stops an Electron powerSaveBlocker id', () => {
  const calls = []
  const fakePowerSaveBlocker = {
    start(type) {
      calls.push(['start', type])
      return 42
    },
    stop(id) {
      calls.push(['stop', id])
    },
    isStarted(id) {
      return id === 42
    }
  }
  const configPath = writeConfig(
    [
      'power:',
      '  prevent_sleep:',
      '    enabled: true',
      '    surfaces: [desktop]',
      '    mode: system',
      ''
    ].join('\n')
  )

  const controller = createPowerSaveBlockerController(fakePowerSaveBlocker, { configPath, surface: 'desktop' })

  assert.equal(controller.start(), true)
  assert.equal(controller.isStarted(), true)
  assert.deepEqual(calls, [['start', 'prevent-app-suspension']])

  controller.stop()
  assert.deepEqual(calls, [
    ['start', 'prevent-app-suspension'],
    ['stop', 42]
  ])
})

test('controller is a no-op when disabled', () => {
  const fakePowerSaveBlocker = {
    start() {
      throw new Error('should not start')
    }
  }

  const controller = createPowerSaveBlockerController(fakePowerSaveBlocker, { block: { enabled: false }, surface: 'desktop' })

  assert.equal(controller.start(), false)
  assert.equal(controller.isStarted(), false)
})
