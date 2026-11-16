import React from 'react';
import ReactDOM from 'react-dom/client';
import {Toaster} from 'sonner';
import Workbench from './workbench';
import './style.css';
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><Workbench/><Toaster richColors position="top-right" closeButton/></React.StrictMode>);
