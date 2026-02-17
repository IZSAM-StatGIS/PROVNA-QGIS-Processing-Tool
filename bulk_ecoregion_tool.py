from qgis.core import (QgsProcessing,
                       QgsProcessingAlgorithm,
                       QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField,
                       QgsProcessingParameterEnum,
                       QgsProcessingParameterFileDestination,
                       QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform,
                       QgsProcessingException)
import requests
import json
import pandas as pd
import re

class EcoregionalizzazioneBulk_V2(QgsProcessingAlgorithm):
    INPUT_LAYER = 'INPUT_LAYER'
    ID_FIELD = 'ID_FIELD'
    DATASETS = 'DATASETS'
    YEARS_FILTER = 'YEARS_FILTER'
    OUTPUT_CSV = 'OUTPUT_CSV'

    DATASET_MAP = [
        {'label': 'Ecoregions (55 classes)', 'id': 'be1e61d7-f9c7-488c-985f-cd97f7e7a04b'},
        {'label': 'Ecoregions (94 classes)', 'id': '130cddc5-ce47-45b2-abfe-0961e3e597cd'},
        {'label': 'Ecoregions (1600 classes)', 'id': '1d215c20-45e1-4e9f-b9d3-df66134586b3'},
        {'label': 'Ecoregions (3600 classes)', 'id': '5c59b5a3-a6f6-4697-a2a9-b7b215d1f862'}
    ]
    
    YEAR_OPTIONS = ['2018', '2019', '2020', '2021', '2022', '2023', '2024']

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT_LAYER, '1. Input points layer', [QgsProcessing.TypeVectorPoint]))
        
        self.addParameter(QgsProcessingParameterField(
            self.ID_FIELD, '2. ID Field (Optional - if empty uses row index)', 
            parentLayerParameterName=self.INPUT_LAYER, 
            type=QgsProcessingParameterField.Any, optional=True))
        
        dataset_labels = [d['label'] for d in self.DATASET_MAP]
        self.addParameter(QgsProcessingParameterEnum(
            self.DATASETS, '3. Select ecoregion datasets', options=dataset_labels, 
            allowMultiple=True, defaultValue=[1, 3]))
        
        self.addParameter(QgsProcessingParameterEnum(
            self.YEARS_FILTER, '4. Select years', 
            options=self.YEAR_OPTIONS, 
            allowMultiple=True, defaultValue=[])) # Nessuna preselezione
        
        self.addParameter(QgsProcessingParameterFileDestination(
            self.OUTPUT_CSV, '5. Output CSV file path', 'CSV files (*.csv)'))

    def processAlgorithm(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.INPUT_LAYER, context)
        id_field = self.parameterAsString(parameters, self.ID_FIELD, context)
        selected_ds_indices = self.parameterAsEnums(parameters, self.DATASETS, context)
        selected_yr_indices = self.parameterAsEnums(parameters, self.YEARS_FILTER, context)
        output_file = self.parameterAsFileOutput(parameters, self.OUTPUT_CSV, context)

        # Se non viene selezionato nulla, target_years rimane vuoto -> estrae tutto
        target_years = [self.YEAR_OPTIONS[i] for i in selected_yr_indices]
        active_configs = [self.DATASET_MAP[i] for i in selected_ds_indices]
        
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = QgsCoordinateTransform(source.sourceCrs(), target_crs, context.transformContext())

        points_data = []
        features = source.getFeatures()
        for i, f in enumerate(features):
            if feedback.isCanceled(): break
            geom = f.geometry()
            if geom.isNull(): continue
            geom.transform(transform)
            pt = geom.asPoint()
            val_id = f[id_field] if id_field and id_field in f.fields().names() else i
            points_data.append({'id': val_id, 'lat': pt.y(), 'lon': pt.x()})

        if not points_data:
            raise QgsProcessingException("No valid points found in input layer.")

        all_rows = []
        BATCH_SIZE = 30 

        for config in active_configs:
            if feedback.isCanceled(): break
            path_id = config["id"]
            model_label = config["label"]
            feedback.pushInfo(f"Querying dataset: {model_label}")
            
            try:
                url_meta = f"https://api.ellipsis-drive.com/v3/path/{path_id}"
                meta_res = requests.get(url_meta).json()
                timestamps = meta_res.get('raster', {}).get('timestamps', [])
                
                for ts in timestamps:
                    if feedback.isCanceled(): break
                    ts_id = ts['id']
                    
                    desc = ts.get('description', '')
                    year_match = re.search(r'\d{4}', desc)
                    year = year_match.group(0) if year_match else "N/A"
                    
                    # Logica: se target_years è vuoto, allora processa tutti (year in target_years è saltato)
                    if target_years and year not in target_years:
                        continue
                    
                    feedback.pushInfo(f"  > Fetching data for year {year}...")
                    
                    for i in range(0, len(points_data), BATCH_SIZE):
                        if feedback.isCanceled(): break
                        batch_subset = points_data[i : i + BATCH_SIZE]
                        locations_list = [[p['lon'], p['lat']] for p in batch_subset]
                        
                        url_location = f"https://api.ellipsis-drive.com/v3/path/{path_id}/raster/timestamp/{ts_id}/location"
                        resp = requests.get(url_location, params={"locations": json.dumps(locations_list)})
                        
                        if resp.status_code == 200:
                            api_results = resp.json()
                            for idx, res in enumerate(api_results):
                                val = res[0] if isinstance(res, list) else (res.get('value') if isinstance(res, dict) else res)
                                all_rows.append({
                                    'ID_ORIGINAL': batch_subset[idx]['id'],
                                    'DATASET': model_label,
                                    'YEAR': year,
                                    'VALUE': val,
                                    'LAT': batch_subset[idx]['lat'],
                                    'LON': batch_subset[idx]['lon']
                                })
                        else:
                            feedback.reportError(f"Error in batch {i} for year {year}: {resp.status_code}")

            except Exception as e:
                feedback.reportError(f"Critical error on {model_label}: {str(e)}")

        if all_rows:
            df_final = pd.DataFrame(all_rows)
            if not output_file.lower().endswith('.csv'):
                output_file += '.csv'
            df_final.to_csv(output_file, index=False, sep=';')
            feedback.pushInfo(f"Processing finished. File saved: {output_file}")
        
        return {self.OUTPUT_CSV: output_file}

    def name(self): return 'ecoregionalizzazione_bulk_v2'
    def displayName(self): return 'Bulk Ecoregionalization - BETA Version 2.2'
    def group(self): return 'IZS Tools'
    def groupId(self): return 'izstools'

    def createInstance(self): return EcoregionalizzazioneBulk_V2()
