import { PGlite } from '@electric-sql/pglite'
import { PGLiteSocketServer } from '@electric-sql/pglite-socket'

// 本地开发用 PostgreSQL（PGlite，数据落盘在 ./data）。
// 用法：node serve.mjs  （保持进程常驻，然后另起 server.py）
const db = new PGlite('./data')
// 漏洞（敏感数据写入日志）：debug 会把线路协议与 SQL 连同参数（口令哈希、会话 token、用户数据）
// 打到控制台；默认关闭，需要排查时用 PGSRV_DEBUG=1 临时开启。
const server = new PGLiteSocketServer({
  db,
  port: 5433,
  host: '127.0.0.1',
  maxConnections: 16,
  debug: process.env.PGSRV_DEBUG === '1',
})
await server.start()
// 漏洞（凭据硬编码到日志）：原先把连接口令直接打印出来；改为只提示连接地址。
console.log('PGlite PostgreSQL listening on 127.0.0.1:5433 (db=postgres)')
