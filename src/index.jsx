import React from 'react';
import ReactDOM from 'react-dom/client';
import Dashboard, { DEMO } from './Dashboard';
import LoginGate from './LoginGate';

const root = ReactDOM.createRoot(document.getElementById('root'));
// The demo build (.env.demo) has sample data and no database, so it has nothing to guard.
root.render(DEMO ? <Dashboard /> : <LoginGate><Dashboard /></LoginGate>);
