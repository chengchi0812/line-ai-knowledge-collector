export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-Hant"><body style={{fontFamily:"system-ui, sans-serif",maxWidth:760,margin:"40px auto",padding:"0 20px",lineHeight:1.7}}>{children}</body></html>;
}
