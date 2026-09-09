import {build} from 'esbuild';
import {mkdir,copyFile} from 'node:fs/promises';
await mkdir('dist',{recursive:true});
await build({entryPoints:['src/main.tsx'],bundle:true,minify:true,sourcemap:false,outdir:'dist',entryNames:'app',define:{'process.env.NODE_ENV':'"production"'},jsx:'automatic',target:['es2022']});
await copyFile('index.html','dist/index.html');
