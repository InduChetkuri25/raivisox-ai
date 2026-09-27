import type { Metadata } from 'next';
import './globals.css';
import { AuthProvider } from '@/components/providers';

export const metadata: Metadata = {
  title: 'RAIVISOX AI - Next-Gen Adaptive Digital Intelligence',
  description: 'From Intent to Verified Outcome. RAIVISOX transforms natural-language human intent into real, completed, and verified digital outcomes.',
  icons: {
    icon: [
      {
        url: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect fill="%234c6ef5" rx="20" width="100" height="100"/><text x="50" y="70" font-size="60" text-anchor="middle" fill="white" font-family="system-ui" font-weight="bold">R</text></svg>',
        type: 'image/svg+xml',
      },
    ],
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-dark-900 text-gray-100 antialiased">
        <AuthProvider>
          {children}
        </AuthProvider>
      </body>
    </html>
  );
}
