import type { Metadata } from "next";
import "./globals.css";
export const metadata:Metadata={title:"一起到 · 异地好友会合旅行",description:"从不同城市出发，一起找到轻松相聚的目的地。"};
export default function RootLayout({children}:{children:React.ReactNode}){return <html lang="zh-CN"><body>{children}</body></html>}
