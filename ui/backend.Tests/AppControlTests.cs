using MicroserviceScripts.Ui.Compose;
using MicroserviceScripts.Ui.Infrastructure;

namespace MicroserviceScripts.Ui.Tests;

public class AppControlTests
{
    /// <summary>Answers `docker compose ls` with a fixed listing.</summary>
    private sealed class ComposeLs(string json) : IProcessRunner
    {
        public Task<ProcessResult> RunAsync(string fileName, IReadOnlyList<string> arguments, string? workingDirectory = null,
            TimeSpan? timeout = null, CancellationToken cancellationToken = default) =>
            Task.FromResult(new ProcessResult(0, json, ""));
    }

    private static AppControl Control(string ls) =>
        new(new ComposeCli(new ComposeLs(ls)), new HttpClient { Timeout = TimeSpan.FromSeconds(2) });

    [Fact]
    public async Task Says_which_projects_are_up()
    {
        var control = Control("""
            [{"Name":"shop","Status":"running(5), exited(1)","ConfigFiles":"/r/shop/compose.yaml"},
             {"Name":"billing","Status":"exited(3)","ConfigFiles":"/r/billing/compose.yaml"}]
            """);
        var projects = new List<(ProjectSelection, string)>
        {
            (new ProjectSelection("/r/shop", ["/r/shop/compose.yaml"]), "shop"),
            (new ProjectSelection("/r/billing", ["/r/billing/compose.yaml"]), "billing"),
            (new ProjectSelection("/r/auth", ["/r/auth/compose.yaml"]), "auth"),
        };

        var status = await control.StatusAsync(projects, null, default);

        Assert.Equal(new AppProjectStatus("shop", "partly", 5, 1, "running(5), exited(1)"), status.Projects[0]);
        Assert.Equal("stopped", status.Projects[1].State);
        Assert.Equal("missing", status.Projects[2].State);
        Assert.False(status.Running);
    }

    [Fact]
    public async Task Up_when_every_project_runs_and_says_whether_its_address_answers()
    {
        var control = Control("""[{"Name":"shop","Status":"running(4)","ConfigFiles":""}]""");
        var status = await control.StatusAsync(
            [(new ProjectSelection("/r/shop", ["/r/shop/compose.yaml"]), "shop")], "http://127.0.0.1:1", default);

        Assert.True(status.Running);
        Assert.Null(status.BaseStatus);   // nothing listens on port 1
    }

    [Fact]
    public void The_command_to_start_it_by_hand_uses_the_chosen_files()
    {
        var command = AppControl.Command(new ProjectSelection(
            "/home/me/my shop", ["/home/me/my shop/compose.yaml", "/home/me/my shop/compose.otel.yaml"], "shop-dev"));

        Assert.Equal("cd \"/home/me/my shop\"\ndocker compose -f compose.yaml -f compose.otel.yaml -p shop-dev up -d", command);
    }
}
