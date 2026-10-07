import React from 'react'
import ReactDOM from 'react-dom/client'
import { MotionConfig } from 'motion/react'
import { Toaster } from 'sonner'
import { App } from './App'
import './styles.css'
import { AuthProvider } from './lib/auth'
import { WorkspaceProvider } from './lib/workspace'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <MotionConfig reducedMotion="user">
      <AuthProvider>
        <WorkspaceProvider>
          <App />
        </WorkspaceProvider>
      </AuthProvider>
      <Toaster theme="dark" position="bottom-right" richColors closeButton />
    </MotionConfig>
  </React.StrictMode>,
)
