import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { test, expect } from './test'
import { buildAppEnv, createSandbox, launchDesktop, writeMockProviderConfig, writeEnvFile } from './fixtures'

// Opt-in real disk-plugin contract test. No SDK/renderer mocks or injected stores.
// GITHERMES_PLUGIN points at the unmodified desktop/plugin.js from GitHermes.
const pluginFile = process.env.GITHERMES_PLUGIN

test('GitHermes native X and titlebar preserve plugin/navigation', async ({}, testInfo) => {
  test.skip(!pluginFile, 'Set GITHERMES_PLUGIN to a GitHermes desktop/plugin.js checkout')
  test.setTimeout(180_000)
  const sandbox = createSandbox('plugin-close')
  const out = process.env.PANE_CLOSE_EVIDENCE || testInfo.outputDir
  fs.mkdirSync(out, { recursive: true })
  const pluginDir = path.join(sandbox.hermesHome, 'desktop-plugins', 'githermes')
  fs.mkdirSync(pluginDir, { recursive: true })
  fs.copyFileSync(pluginFile!, path.join(pluginDir, 'plugin.js'))
  const packageRoot = path.resolve(path.dirname(pluginFile!), '..')
  const backendDir = path.join(sandbox.hermesHome, 'plugins', 'githermes')
  fs.mkdirSync(backendDir, { recursive: true })
  fs.copyFileSync(path.join(packageRoot, 'plugin.yaml'), path.join(backendDir, 'plugin.yaml'))
  fs.cpSync(path.join(packageRoot, 'dashboard'), path.join(backendDir, 'dashboard'), { recursive: true })
  writeMockProviderConfig(sandbox.hermesHome, 'http://127.0.0.1:1', undefined, 'plugins:\n  enabled: [githermes]')
  writeEnvFile(sandbox.hermesHome)
  const env = buildAppEnv(sandbox, {
    HOME: sandbox.root,
    HERMES_REAL_HOME: sandbox.root,
    GH_CONFIG_DIR: path.join(sandbox.root, 'gh'),
    TERMINAL_HOME_MODE: 'isolated',
    SHELL: '/bin/false'
  })
  const repoRoot = path.resolve(import.meta.dirname, '../../..')
  const seed = spawnSync(
    process.env.HERMES_DESKTOP_PYTHON || path.join(repoRoot, '.venv/bin/python'),
    [
      '-c',
      `from hermes_state import SessionDB
s=SessionDB()
s.create_session('pane-close-smoke', 'cli', cwd=${JSON.stringify(sandbox.root)})
s.append_message('pane-close-smoke', 'user', 'Pane close smoke')
s.append_message('pane-close-smoke', 'assistant', 'Ready for pane close verification.')
s.set_session_title('pane-close-smoke', 'Pane close smoke')
s.close()
`
    ],
    { env, cwd: repoRoot, encoding: 'utf8' }
  )
  expect(seed.status, seed.stderr).toBe(0)
  const { app, page } = await launchDesktop(env)
  try {
    const toggle = page.locator('button[aria-label="GitHub"]')
    await toggle.waitFor({ timeout: 120_000 })
    await expect(page.getByText('Waiting for Hermes backend to launch', { exact: true })).toHaveCount(0, {
      timeout: 120_000
    })
    await page.getByText('Pane close smoke', { exact: true }).first().click()
    const showRight = page.getByRole('button', { name: 'Show right sidebar', exact: true })
    if (await showRight.isVisible()) await showRight.click()
    const tab = page.locator('[data-tree-tab="githermes:pane"]')
    if (!(await tab.isVisible())) await toggle.click()
    await tab.waitFor({ state: 'visible' })
    const groupOf = (selector: string) =>
      page.locator(selector).evaluate(element => element.closest('[data-tree-group]')?.getAttribute('data-tree-group'))
    const files = page.locator('[data-tree-tab="files"]')
    await expect(files).toBeVisible()
    expect(await groupOf('[data-tree-tab="githermes:pane"]')).toBe(await groupOf('[data-tree-tab="files"]'))
    await tab.click()
    await expect(tab).toHaveAttribute('aria-selected', 'true')
    await tab.hover()
    await page.screenshot({ path: path.join(out, '01-open.png') })
    const decisions = () =>
      page.evaluate(() => JSON.parse(localStorage.getItem('hermes.desktop.pluginDecisions.v2') || '{}'))
    const before = await decisions()
    expect(before.githermes).not.toBe(false)
    const nav = page.locator('[data-sidebar="menu-button"]').filter({ hasText: /^GitHub$/ })
    await expect(nav).toBeVisible()
    await tab.hover()
    await tab.getByRole('button', { name: /close/i }).click()
    await expect(tab).toHaveCount(0)
    await expect(nav).toBeVisible()
    await expect(toggle).toBeVisible()
    expect(await decisions()).toEqual(before)
    fs.writeFileSync(
      path.join(out, 'after-x-state.json'),
      JSON.stringify(
        await page.evaluate(() => ({
          decisions: JSON.parse(localStorage.getItem('hermes.desktop.pluginDecisions.v2') || '{}'),
          dismissed: JSON.parse(localStorage.getItem('hermes.desktop.dismissedPanes.v1') || '[]'),
          tabs: Array.from(document.querySelectorAll('[data-tree-tab]'), element =>
            element.getAttribute('data-tree-tab')
          )
        })),
        null,
        2
      )
    )
    await page.screenshot({ path: path.join(out, '02-x-hidden.png') })
    await page.reload()
    await expect(toggle).toBeVisible()
    await expect(nav).toBeVisible()
    await expect(tab).toHaveCount(0)
    expect(await decisions()).toEqual(before)
    await toggle.click()
    await expect(tab).toBeVisible()
    await page.screenshot({ path: path.join(out, '03-reopened.png') })
    await toggle.click()
    await expect(tab).toHaveCount(0)
    await toggle.click()
    await expect(tab).toBeVisible()
    expect(await decisions()).toEqual(before)
    await page.screenshot({ path: path.join(out, '04-toggle-reopened.png') })
  } catch (error) {
    fs.writeFileSync(path.join(out, 'failure-body.txt'), await page.locator('body').innerText())
    await page.screenshot({ path: path.join(out, 'failure.png') })
    throw error
  } finally {
    await app.close()
    sandbox.cleanup()
  }
})
