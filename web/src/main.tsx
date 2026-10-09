import React from 'react'
import ReactDOM from 'react-dom/client'
import { MotionConfig } from 'motion/react'
import { Toaster } from 'sonner'
import { App } from './App'
import './styles.css'
import './styles/app.css'
import { AuthProvider } from './lib/auth'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <MotionConfig reducedMotion="user">
      <AuthProvider>
        <App />
      </AuthProvider>
      <Toaster theme="system" position="bottom-right" richColors closeButton />
    </MotionConfig>
  </React.StrictMode>,
)
