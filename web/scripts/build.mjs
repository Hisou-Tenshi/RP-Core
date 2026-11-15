// SPDX-License-Identifier: AGPL-3.0-only
import {readFile, mkdir, writeFile} from 'node:fs/promises';

// Keep the web package independently deployable. Its runtime reads the canonical
// public catalog remotely; the build bundles only its validation schema.
const source = new URL('../../src/rp_core/compat_runtime/model-config.schema.json', import.meta.url);
const target = new URL('../lib/model-config.schema.json', import.meta.url);
try {
  const content = await readFile(source, 'utf8');
  await mkdir(new URL('../lib/', import.meta.url), {recursive: true});
  await writeFile(target, content);
} catch (error) {
  if (error.code !== 'ENOENT') throw error;
  await readFile(target, 'utf8');
}
console.log('Console ready; canonical catalog is loaded from the configured public repository.');
