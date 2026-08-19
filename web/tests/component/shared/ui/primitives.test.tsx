import { Alert, AlertDescription, AlertTitle } from "@shared/ui/alert";
import { Badge } from "@shared/ui/badge";
import { Panel, PanelContent, PanelDescription, PanelHeader, PanelTitle } from "@shared/ui/panel";
import * as TabsNamespace from "@shared/ui/tabs";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@shared/ui/tabs";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => cleanup());

describe("shared UI primitives", () => {
  it("renders badge, alert, and panel primitives with caller classes", () => {
    const { container } = render(
      <Panel className="custom-panel">
        <PanelHeader className="custom-panel-header">
          <PanelTitle>Status</PanelTitle>
          <PanelDescription>Queue health</PanelDescription>
        </PanelHeader>
        <PanelContent>
          <Badge variant="secondary" className="custom-badge">
            Ready
          </Badge>
          <Alert variant="destructive" className="custom-alert">
            <AlertTitle>Provider degraded</AlertTitle>
            <AlertDescription>Retry later.</AlertDescription>
          </Alert>
        </PanelContent>
      </Panel>,
    );

    expect(container.querySelector('[data-slot="panel"]')).toHaveClass("custom-panel");
    expect(container.querySelector('[data-slot="panel-header"]')).toHaveClass(
      "custom-panel-header",
    );
    expect(screen.getByText("Ready")).toHaveAttribute("data-slot", "badge");
    expect(screen.getByText("Ready")).toHaveClass("custom-badge");
    expect(screen.getByRole("alert")).toHaveClass("custom-alert");
    expect(screen.getByText("Provider degraded")).toHaveAttribute("data-slot", "alert-title");
    expect(screen.getByText("Retry later.")).toHaveAttribute("data-slot", "alert-description");
  });

  it("exposes shadcn-style named Tabs exports", () => {
    render(
      <Tabs defaultValue="overview">
        <TabsList aria-label="Named tabs" className="named-list">
          <TabsTrigger value="overview" className="named-trigger">
            Overview
          </TabsTrigger>
          <TabsTrigger value="details">Details</TabsTrigger>
        </TabsList>
        <TabsContent value="overview" className="named-content">
          Named overview
        </TabsContent>
        <TabsContent value="details">Named details</TabsContent>
      </Tabs>,
    );

    expect(screen.getByRole("tablist", { name: "Named tabs" })).toHaveClass("named-list");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveClass("named-trigger");
    expect(screen.getByText("Named overview")).toHaveClass("named-content");
  });

  it("keeps Radix Tabs behavior behind the shared wrapper", () => {
    render(
      <TabsNamespace.Root defaultValue="overview">
        <TabsNamespace.List aria-label="News modules" className="custom-tabs-list">
          <TabsNamespace.Trigger value="overview" className="custom-trigger">
            Overview
          </TabsNamespace.Trigger>
          <TabsNamespace.Trigger value="signals">Signals</TabsNamespace.Trigger>
        </TabsNamespace.List>
        <TabsNamespace.Content value="overview" className="custom-content">
          Overview panel
        </TabsNamespace.Content>
        <TabsNamespace.Content value="signals">Signals panel</TabsNamespace.Content>
      </TabsNamespace.Root>,
    );

    expect(screen.getByRole("tablist", { name: "News modules" })).toHaveClass("custom-tabs-list");
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveClass("custom-trigger");
    expect(screen.getByText("Overview panel")).toBeVisible();

    fireEvent.mouseDown(screen.getByRole("tab", { name: "Signals" }), {
      button: 0,
      ctrlKey: false,
    });

    expect(screen.getByText("Signals panel")).toBeVisible();
  });
});
