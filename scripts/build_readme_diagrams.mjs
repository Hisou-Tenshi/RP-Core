// SPDX-License-Identifier: AGPL-3.0-only
// README Mermaid blocks are authoritative; PNGs and their manifest are generated.
import {createHash} from 'node:crypto';
import {readFile, writeFile, mkdir, mkdtemp, rm, copyFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const directory = join(root, 'docs/diagrams');
const languages = [['zh', 'README.md'], ['en', 'README.en.md'], ['ja', 'README.ja.md']];
const kinds = ['architecture', 'execution'];
const inputs = [
  'scripts/build_readme_diagrams.mjs',
  'docs/diagrams/mermaid.config.json',
  'docs/diagrams/package.json',
  'docs/diagrams/package-lock.json',
];
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const check = process.argv.includes('--check');
if (process.argv.slice(2).some(argument => argument !== '--check')) {
  throw new Error('Usage: node scripts/build_readme_diagrams.mjs [--check]');
}

function dimensions(bytes) {
  if (!bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
      || bytes.toString('ascii', 12, 16) !== 'IHDR') throw new Error('Expected a PNG image');
  return {width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20)};
}

async function sources() {
  const result = [];
  for (const [language, source] of languages) {
    const markdown = await readFile(join(root, source), 'utf8');
    const blocks = [...markdown.matchAll(/<!-- rp-diagram: ([a-z]+\.[a-z]+) -->\s*```mermaid\n([\s\S]*?)\n```/g)];
    if (blocks.length !== kinds.length || [...markdown.matchAll(/^```mermaid$/gm)].length !== kinds.length) {
      throw new Error(`${source}: expected exactly two labeled Mermaid blocks`);
    }
    for (const kind of kinds) {
      const id = `${kind}.${language}`;
      const matches = blocks.filter(block => block[1] === id);
      if (matches.length !== 1) throw new Error(`${source}: missing or duplicate ${id}`);
      const image = `docs/diagrams/${id}.png`;
      if (!markdown.includes(`](${image})`)) throw new Error(`${source}: missing image reference ${image}`);
      result.push({id, source, source_sha256: sha256(matches[0][2]), image, code: matches[0][2]});
    }
  }
  return result;
}

async function execute(command, args) {
  await new Promise((accept, reject) => {
    const child = spawn(command, args, {cwd: root, stdio: ['ignore', 'pipe', 'pipe']});
    let output = '';
    const capture = chunk => { output = (output + chunk).slice(-12000); };
    child.stdout.on('data', capture);
    child.stderr.on('data', capture);
    child.on('error', reject);
    child.on('close', code => code === 0 ? accept() : reject(new Error(`Diagram renderer failed (${code}): ${output}`)));
  });
}

const diagrams = await sources();
const generation_inputs = Object.fromEntries(await Promise.all(inputs.map(async name =>
  [name, sha256(await readFile(join(root, name)))]
)));

if (check) {
  const manifest = JSON.parse(await readFile(join(directory, 'manifest.json'), 'utf8'));
  if (manifest.schema_version !== 1 || JSON.stringify(manifest.generation_inputs) !== JSON.stringify(generation_inputs)
      || manifest.diagrams?.length !== diagrams.length) {
    throw new Error('Diagram build inputs changed. Regenerate README images.');
  }
  for (const diagram of diagrams) {
    const entry = manifest.diagrams.find(item => item.id === diagram.id);
    const bytes = await readFile(join(root, diagram.image));
    const size = dimensions(bytes);
    if (!entry || entry.source !== diagram.source || entry.source_sha256 !== diagram.source_sha256
        || entry.image !== diagram.image || entry.image_sha256 !== sha256(bytes)
        || entry.bytes !== bytes.length || entry.width !== size.width || entry.height !== size.height) {
      throw new Error(`${diagram.id}: image/source mismatch. Regenerate README images.`);
    }
  }
  console.log('Verified six README images against their authoritative Mermaid sources and build inputs.');
} else {
  const temporary = await mkdtemp(join(tmpdir(), 'rp-readme-diagrams-'));
  try {
    const packageRoot = join(directory, 'node_modules/@mermaid-js/mermaid-cli');
    const cliPackage = JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8'));
    const cli = join(packageRoot, cliPackage.bin.mmdc);
    const puppeteer = {args: ['--disable-dev-shm-usage']};
    if (process.env.PUPPETEER_EXECUTABLE_PATH) puppeteer.executablePath = process.env.PUPPETEER_EXECUTABLE_PATH;
    const puppeteerConfig = join(temporary, 'puppeteer.json');
    await writeFile(puppeteerConfig, JSON.stringify(puppeteer));
    await mkdir(directory, {recursive: true});
    const manifest = {schema_version: 1, generation_inputs, diagrams: []};
    // Render sequentially to avoid six simultaneous browser processes.
    for (const diagram of diagrams) {
      const input = join(temporary, `${diagram.id}.mmd`);
      const output = join(temporary, `${diagram.id}.png`);
      await writeFile(input, diagram.code + '\n');
      await execute(process.execPath, [cli, '--input', input, '--output', output,
        '--configFile', join(directory, 'mermaid.config.json'), '--puppeteerConfigFile', puppeteerConfig,
        '--backgroundColor', 'white', '--width', '1800', '--height', '1200', '--scale', '1.5']);
      const bytes = await readFile(output);
      if (bytes.length > 2_000_000) throw new Error(`${diagram.id}: image exceeds the public asset limit`);
      const {code, ...entry} = diagram;
      manifest.diagrams.push({...entry, image_sha256: sha256(bytes), bytes: bytes.length, ...dimensions(bytes)});
      await copyFile(output, join(root, diagram.image));
      console.log(`Rendered ${diagram.image}`);
    }
    await writeFile(join(directory, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  } finally {
    await rm(temporary, {recursive: true, force: true});
  }
}
