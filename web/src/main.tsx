import React from 'react';
import { createRoot } from 'react-dom/client';
import './globals.css';
import './theme.css';
import { App } from './App';

// Light and dark follow the OS.
const dark = window.matchMedia('(prefers-color-scheme: dark)');
const sync = () => document.documentElement.classList.toggle('dark', dark.matches);
sync();
dark.addEventListener('change', sync);

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
