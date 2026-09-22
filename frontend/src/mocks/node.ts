import { setupServer } from 'msw/node';
import { handlers } from './handlers';

// MSW Node server for Vitest or other Node environments
export const server = setupServer(...handlers);
export default server;
