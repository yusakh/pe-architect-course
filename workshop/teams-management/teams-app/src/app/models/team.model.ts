// src/app/models/team.model.ts
export interface Team {
  id: string;
  name: string;
  created_at: string;
}

export interface TeamCreate {
  name: string;
}

export interface RolloutTemplate {
  name: string;
  image: string;
  strategy: string;
  replicas: number;
}

export interface RolloutDeployment {
  name: string;
  namespace: string;
  templateRef: string;
  status: { phase: string; message?: string; rolloutName?: string };
}
