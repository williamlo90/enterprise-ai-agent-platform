import { createContext } from 'react';
import type { ApiCoordinator,ContextStamp } from '../app/api-coordinator';
export type Workspace={api:ApiCoordinator;stamp:ContextStamp};
export const WorkspaceContext=createContext<Workspace|null>(null);
