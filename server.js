// بطلب المالك (8 نوفمبر 2026): هذا الموقع القديم توقف نهائياً.
// كل زيارة تتحول فوراً إلى الموقع الرسمي nabdalsooq.com (تحويل دائم 301) مع الحفاظ على المسار.
// الكود القديم محفوظ في سجل Git ويمكن استرجاعه عند الحاجة.
const http = require('http');

const MAIN = 'https://nabdalsooq.com';
const PORT = process.env.PORT || 3000;

http.createServer((req, res) => {
  const url = req.url || '/';
  // الواجهات البرمجية القديمة لا تُحوَّل (لا تعمل في الموقع الجديد بنفس الشكل): نرد بأن الخدمة انتقلت
  if (url.startsWith('/api/')) {
    res.writeHead(410, { 'Content-Type': 'application/json; charset=utf-8' });
    return res.end(JSON.stringify({ error: 'moved', message: 'انتقل الموقع إلى nabdalsooq.com', url: MAIN }));
  }
  res.writeHead(301, { Location: MAIN + (url === '/' ? '/' : url), 'Cache-Control': 'public, max-age=86400' });
  res.end();
}).listen(PORT, () => console.log(`↪️ تحويل دائم إلى ${MAIN} على المنفذ ${PORT}`));
