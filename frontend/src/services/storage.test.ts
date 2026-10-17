import { afterEach, describe, expect, it } from 'vitest';
import { clearSessionId, getSessionId, setSessionId } from './storage';

describe('storage gateway', () => {
    const localStorageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');

    afterEach(() => {
        if (localStorageDescriptor) {
            Object.defineProperty(window, 'localStorage', localStorageDescriptor);
        }
    });

    it('falls back safely when localStorage property access is blocked', () => {
        Object.defineProperty(window, 'localStorage', {
            configurable: true,
            get() {
                throw new DOMException('Storage is blocked', 'SecurityError');
            },
        });

        expect(getSessionId()).toBeNull();
        expect(() => setSessionId('session-1')).not.toThrow();
        expect(() => clearSessionId()).not.toThrow();
    });
});
