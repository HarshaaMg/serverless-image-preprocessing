import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

// Runs once before all browser tests: generates the test images (JPEG, PNG, fake,
// oversized, GIF, truncated) with Python/Pillow into e2e/.fixtures/ (git-ignored).
export default function globalSetup() {
  const python = process.platform === 'win32'
    ? path.join(here, '../../backend/.venv/Scripts/python.exe')
    : path.join(here, '../../backend/.venv/bin/python')
  execFileSync(python, [path.join(here, 'make_fixtures.py'), path.join(here, '.fixtures')], { stdio: 'inherit' })
}
