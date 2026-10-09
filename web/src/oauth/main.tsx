import React from 'react'
import ReactDOM from 'react-dom/client'
import '../styles/tokens.css'
import '../styles/mia.css'
import './oauth.css'
import { Consent } from './Consent'

const handle = new URLSearchParams(window.location.search).get('request')

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Consent handle={handle} />
  </React.StrictMode>,
)
