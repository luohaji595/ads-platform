/**
 * 报告正文是 markdown，网页里只把 **粗体** 认出来，别把星号露给用户看。
 *
 * 从 pages/Approval.tsx 里搬出来的同一份实现 —— 那份是页面内私有函数，
 * 跨页面 import 只能靠复制；两份合起来才三行，重复它比让 Approval 去依赖一个新模块更省事。
 */
export default function Rich({ text }: { text: string }) {
  const parts = String(text).split(/\*\*(.+?)\*\*/g)
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? <b key={i}>{part}</b> : <span key={i}>{part}</span>,
      )}
    </>
  )
}