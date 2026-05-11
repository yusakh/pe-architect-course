import { Component, OnInit } from "@angular/core";
import { TeamsService } from "../../services/teams.service";
import { Team, RolloutTemplate, RolloutDeployment } from "../../models/team.model";

@Component({
  selector: "app-team-list",
  templateUrl: "./team-list.component.html",
  styleUrls: ["./team-list.component.css"],
})
export class TeamListComponent implements OnInit {
  teams: Team[] = [];
  isLoading = true;
  errorMessage = "";

  // Deploy modal state
  deployModalTeam: Team | null = null;
  templates: RolloutTemplate[] = [];
  deployments: RolloutDeployment[] = [];
  selectedTemplate = "";
  deployName = "";
  deployError = "";
  deploySuccess = "";
  isDeploying = false;

  constructor(public teamsService: TeamsService) {}

  ngOnInit() {
    this.loadTeams();
  }

  loadTeams() {
    this.isLoading = true;
    this.errorMessage = "";

    this.teamsService.getTeams().subscribe({
      next: (teams) => {
        this.teams = teams;
        this.isLoading = false;
      },
      error: (error) => {
        this.errorMessage = error;
        this.isLoading = false;
      },
    });
  }

  deleteTeam(teamId: string, teamName: string) {
    if (confirm(`Are you sure you want to delete team "${teamName}"?`)) {
      this.teamsService.deleteTeam(teamId).subscribe({
        next: () => this.loadTeams(),
        error: (error) => { this.errorMessage = error; },
      });
    }
  }

  openDeployModal(team: Team) {
    this.deployModalTeam = team;
    this.selectedTemplate = "";
    this.deployName = "";
    this.deployError = "";
    this.deploySuccess = "";
    const ns = this.teamsService.teamNamespace(team.name);

    this.teamsService.getRolloutTemplates().subscribe({
      next: (t) => { this.templates = t; if (t.length) this.selectedTemplate = t[0].name; },
      error: () => { this.deployError = "Could not load templates."; },
    });

    this.teamsService.getDeployments(ns).subscribe({
      next: (d) => { this.deployments = d; },
      error: () => { this.deployments = []; },
    });
  }

  closeDeployModal() {
    this.deployModalTeam = null;
  }

  submitDeploy() {
    if (!this.deployModalTeam || !this.selectedTemplate || !this.deployName) return;
    this.isDeploying = true;
    this.deployError = "";
    this.deploySuccess = "";
    const ns = this.teamsService.teamNamespace(this.deployModalTeam.name);

    this.teamsService.createDeployment(ns, this.deployName, this.selectedTemplate).subscribe({
      next: (d) => {
        this.isDeploying = false;
        this.deploySuccess = `Deployment "${d.name}" created — refreshing status in 5s…`;
        this.deployName = "";
        // Reload from server after operator has had time to process
        setTimeout(() => this.refreshDeployments(), 5000);
      },
      error: (err) => {
        this.isDeploying = false;
        this.deployError = err;
      },
    });
  }

  refreshDeployments() {
    if (!this.deployModalTeam) return;
    const ns = this.teamsService.teamNamespace(this.deployModalTeam.name);
    this.teamsService.getDeployments(ns).subscribe({
      next: (d) => {
        this.deployments = d;
        this.deploySuccess = "";
      },
      error: () => {},
    });
  }

  deleteDeployment(name: string) {
    if (!this.deployModalTeam) return;
    const ns = this.teamsService.teamNamespace(this.deployModalTeam.name);
    this.teamsService.deleteDeployment(ns, name).subscribe({
      next: () => { this.deployments = this.deployments.filter(d => d.name !== name); },
      error: (err) => { this.deployError = err; },
    });
  }

  formatDate(dateString: string): string {
    return new Date(dateString).toLocaleDateString("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
}
