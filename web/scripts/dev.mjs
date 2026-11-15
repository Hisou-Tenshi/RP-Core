// SPDX-License-Identifier: AGPL-3.0-only
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {resolve, extname} from 'node:path';
import handler from '../api/index.mjs';

const localEnv = new URL('../.env.local', import.meta.url);
if (existsSync(localEnv)) process.loadEnvFile(fileURLToPath(localEnv));
const root = fileURLToPath(new URL('../public/', import.meta.url));
const port = Number(process.env.PORT || 3000);
createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${port}`);
  if (url.pathname.startsWith('/api/')) return handler(req,res);
  const path=resolve(root,'.'+(url.pathname==='/'?'/index.html':decodeURIComponent(url.pathname)));
  if (!path.startsWith(root)) {res.writeHead(403);res.end();return;}
  try {
    const body=await readFile(path);
    res.writeHead(200,{'Content-Type':{'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'}[extname(path)] || 'application/octet-stream'});
    res.end(body);
  } catch {res.writeHead(404);res.end();}
}).listen(port,'127.0.0.1',()=>console.log(`RP-Core console: http://localhost:${port}`));
