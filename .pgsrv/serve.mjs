import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

// 本地开发用 PostgreSQL（PGlite，数据落盘在 ./data）。
// 用法：node serve.mjs  （保持进程常驻，然后另起 server.py）
const db = new PGlite('./data')
const server = new PGLiteSocketServer({ db, port: 5433, host: '127.0.0.1', maxConnections: 16, debug: true })
await server.start()
console.log('PGlite PostgreSQL listening on 127.0.0.1:5433 (user=postgres password=postgres db=postgres)')
