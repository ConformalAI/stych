import { useCallback, useState } from 'react';
import { Outlet } from 'react-router-dom';
import Header from '../Header';
import Toast from '../Toast';
import PrimaryRail from './PrimaryRail';
import { getRailExpanded, setRailExpanded } from '../../services/storage';

/**
 * The authenticated application frame: one global header spanning the full
 * width, with the primary rail and routed content below it. Every signed-in
 * surface renders through here, so page chrome is defined once, not per page.
 */
export default function AppShell() {
    const [railExpanded, setRailExpandedState] = useState(getRailExpanded);

    const toggleRail = useCallback(() => {
        setRailExpandedState((prev) => {
            const next = !prev;
            setRailExpanded(next);
            return next;
        });
    }, []);

    return (
        <div
            className="h-screen flex flex-col overflow-hidden"
            style={{ background: 'var(--c-paper)' }}
        >
            <Header isConnected={true} />

            <div className="flex flex-1 min-h-0">
                <PrimaryRail expanded={railExpanded} onToggle={toggleRail} />
                <div className="flex-1 min-w-0">
                    <Outlet />
                </div>
            </div>

            {/* Mounted once for the whole shell so any route can raise a toast. */}
            <Toast />
        </div>
    );
}
