using MicroserviceScripts.Ui.Run;

namespace MicroserviceScripts.Ui.Tests;

public class PreflightTests
{
    [Fact]
    public void A_containers_uptime_is_read_from_dockers_nanosecond_timestamp()
    {
        var now = new DateTimeOffset(2026, 11, 20, 18, 53, 4, TimeSpan.Zero);

        Assert.Equal(TimeSpan.FromSeconds(29.5), Preflight.UpFor("2026-11-20T18:52:34.500000000Z\n", now));
        Assert.Null(Preflight.UpFor("", now));
        Assert.True(Preflight.UpFor("2026-11-20T18:52:34.5Z", now) < Preflight.CadvisorWarmUp);
    }
}
