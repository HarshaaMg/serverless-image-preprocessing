import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'

// Entry point: mount the <App /> component into <div id="root"> in index.html.
// StrictMode runs extra checks in development only (it has no effect in production).
createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
