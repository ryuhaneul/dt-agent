#!/bin/sh
# Regenerate the checked-in browser bundle; never run during deployment.
set -eu
repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
cp "$repo/scripts/vendor/micromark/package.json" "$repo/scripts/vendor/micromark/package-lock.json" "$build_dir/"
cd "$build_dir"
npm ci --ignore-scripts --no-audit --no-fund
mkdir packs
npm pack --ignore-scripts --json --pack-destination packs micromark@4.0.3 micromark-extension-gfm@3.0.0 > packs.json
cat > entry.js <<'JS'
export {micromark} from 'micromark';
export {gfm, gfmHtml} from 'micromark-extension-gfm';
JS
node --input-type=module - "$repo" <<'JS'
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {build} from 'esbuild';
const repo = process.argv[2];
const lock = JSON.parse(fs.readFileSync('package-lock.json', 'utf8'));
const packs = JSON.parse(fs.readFileSync('packs.json', 'utf8'));
const sources = packs.map(pack => {
  if (pack.integrity !== lock.packages[`node_modules/${pack.name}`].integrity) {
    throw new Error(`Tarball integrity mismatch: ${pack.name}`);
  }
  const sha = crypto.createHash('sha256').update(fs.readFileSync(`packs/${pack.filename}`)).digest('hex');
  return `${pack.name}@${pack.version} tarball SHA-256: ${sha}`;
});
const result = await build({
  entryPoints: ['entry.js'], bundle: true, format: 'esm', platform: 'neutral', mainFields: ['module', 'main'],
  target: 'es2020', minify: true, legalComments: 'none', metafile: true, write: false
});
if (Object.values(result.metafile.outputs).some(output => output.imports.length)) {
  throw new Error('The vendor bundle must not have external imports');
}
const packages = [...new Set(Object.keys(result.metafile.inputs).filter(input => input.startsWith('node_modules/'))
  .map(input => input.split('/').slice(0, input.startsWith('node_modules/@') ? 3 : 2).join('/')))].sort();
const notices = packages.map(directory => {
  const info = JSON.parse(fs.readFileSync(`${directory}/package.json`, 'utf8'));
  const license = fs.readdirSync(directory).find(file => /^licen[sc]e(?:\.md|\.txt)?$/i.test(file));
  if (!license || info.license !== 'MIT') throw new Error(`Review license for ${info.name}`);
  return `### ${info.name} ${info.version}\n\nSource: ${lock.packages[directory].resolved}\n\n` +
    fs.readFileSync(path.join(directory, license), 'utf8').trim() + '\n';
});
const banner = `/*\nVendored micromark 4.0.3 + micromark-extension-gfm 3.0.0 (MIT).\n` +
  `Generated with esbuild 0.25.12; command: sh scripts/vendor_micromark.sh\n` +
  sources.join('\n') + '\nFull original MIT notices for bundled packages:\n\n' + notices.join('\n') + '*/\n';
fs.writeFileSync(path.join(repo, 'ductor_bot/webchat/web/static/js/vendor/micromark-gfm.js'),
  banner + result.outputFiles[0].text);
const noticePath = path.join(repo, 'THIRD_PARTY_NOTICES.md');
const marker = '<!-- BEGIN MICROMARK VENDOR NOTICES -->';
const current = fs.readFileSync(noticePath, 'utf8').split(marker)[0].trimEnd();
fs.writeFileSync(noticePath, current + '\n\n' + marker + '\n## Webchat Markdown renderer\n\n' +
  sources.join('\n\n') + '\n\n' + notices.join('\n') + '<!-- END MICROMARK VENDOR NOTICES -->\n');
JS
