import { setupWorker } from 'msw';
import { handlers } from './handlers';

// MSW browser worker for development and browser-based tests
export const worker = setupWorker(...handlers);
export default worker;
