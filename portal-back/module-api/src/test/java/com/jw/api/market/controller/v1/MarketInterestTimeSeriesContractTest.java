package com.jw.api.market.controller.v1;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jw.service.market.dto.v1.Market;
import com.jw.service.market.service.v1.MarketService;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class MarketInterestTimeSeriesContractTest {

    private static final String INCIDENT_BODY = """
        {
          "view": "strategic_ml",
          "market_id": "ml_006",
          "selected_brand": "리바로젯",
          "visit_location": "전체",
          "specialty": "전체",
          "filters": {
            "atc": { "atc4": [] },
            "channel": { "visit_location": [], "specialty": [] }
          }
        }
        """;

    @Test
    void acceptsIncidentScalarFiltersAtTheBffBoundary() throws Exception {
        // Frontend unit tests did not detect the 2026-08-11 incident; this contract test closes that gap.
        MarketService service = mock(MarketService.class);
        var mockMvc = MockMvcBuilders.standaloneSetup(new MarketController(service)).build();

        mockMvc.perform(post("/api/v1/market/brand/interest/time/series")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content(INCIDENT_BODY))
            .andExpect(status().isOk());

        var request = captureRequest(service);
        assertThat(request.getVisit_location()).isEqualTo(List.of("전체"));
        assertThat(request.getSpecialty()).isEqualTo(List.of("전체"));
    }

    @Test
    void preservesExistingArrayFiltersAtTheBffBoundary() throws Exception {
        MarketService service = mock(MarketService.class);
        var mockMvc = MockMvcBuilders.standaloneSetup(new MarketController(service)).build();
        String arrayBody = INCIDENT_BODY
            .replace("\"visit_location\": \"전체\"", "\"visit_location\": [\"전체\"]")
            .replace("\"specialty\": \"전체\"", "\"specialty\": [\"전체\"]");

        mockMvc.perform(post("/api/v1/market/brand/interest/time/series")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content(arrayBody))
            .andExpect(status().isOk());

        var request = captureRequest(service);
        assertThat(request.getVisit_location()).isEqualTo(List.of("전체"));
        assertThat(request.getSpecialty()).isEqualTo(List.of("전체"));
    }

    @Test
    void preservesStrategicIdentityAtTheBffBoundary() throws Exception {
        MarketService service = mock(MarketService.class);
        var mockMvc = MockMvcBuilders.standaloneSetup(new MarketController(service)).build();
        String arrayBody = INCIDENT_BODY
            .replace("\"visit_location\": \"전체\"", "\"visit_location\": [\"전체\"]")
            .replace("\"specialty\": \"전체\"", "\"specialty\": [\"전체\"]")
            .replace("\"market_id\": \"ml_006\"", "\"market_id\": \"ml_006\", \"source\": \"iqvia_nsa\"");

        mockMvc.perform(post("/api/v1/market/brand/interest/time/series")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content(arrayBody))
            .andExpect(status().isOk());

        var request = captureRequest(service);
        assertThat(request.getMarket_id()).isEqualTo("ml_006");
        assertThat(request.getSource()).isEqualTo("iqvia_nsa");
    }

    @Test
    void preservesExplicitNullFiltersWithoutInventingEmptyLists() throws Exception {
        MarketService service = mock(MarketService.class);
        var mockMvc = MockMvcBuilders.standaloneSetup(new MarketController(service)).build();
        String nullBody = INCIDENT_BODY
            .replace("\"visit_location\": \"전체\"", "\"visit_location\": null")
            .replace("\"specialty\": \"전체\"", "\"specialty\": null");

        mockMvc.perform(post("/api/v1/market/brand/interest/time/series")
                .header("Authorization-Access-Token", "portal-token")
                .contentType(MediaType.APPLICATION_JSON)
                .content(nullBody))
            .andExpect(status().isOk());

        var request = captureRequest(service);
        assertThat(request.getVisit_location()).isNull();
        assertThat(request.getSpecialty()).isNull();
    }

    private static Market.Request.Interest.TimeSeries captureRequest(MarketService service) {
        var captor = ArgumentCaptor.forClass(Market.Request.Interest.TimeSeries.class);
        verify(service).getInterestTimeseries(eq("portal-token"), captor.capture());
        return captor.getValue();
    }

}
